#!/usr/bin/env python

import logging, os, re, signal, socket, subprocess as SUB, sys, time

from multiprocessing import Process as mpProcess, Queue as mpQueue

from Queue import Queue, Empty
from threading import BoundedSemaphore, Thread

myHostname = socket.gethostname()
myPid = os.getpid()

dbcomment = re.compile('^\s*(#|\n|$)')
dbbarrier = re.compile('^#DISBATCH BARRIER(?: ([^\n]+)?)?\n', re.I)
# would it make sense to allow an (optional) command after the repeat?
dbrepeat  = re.compile('^#DISBATCH REPEAT\s+(?P<repeat>[0-9]+)(?:\s+start\s+(?P<start>[0-9]+))?(?:\s+step\s+(?P<step>[0-9]+))?(?: (?P<command>[^\n]+))?\s*\n', re.I)
dbprefix  = re.compile('^#DISBATCH PREFIX ([^\n]+)\n', re.I)
dbsuffix  = re.compile('^#DISBATCH SUFFIX ([^\n]+)\n', re.I)

# Special ID for "out of band" task events
TaskIdOOB = -1
CmdPoison = '!!Poison!!'
CmdRetire = '!!Retire Me!!'

# TODO: Because of the way SLURM stages batch scripts, it is difficult to infer the correct path.
ScriptPath = '/mnt/xfs1/home/carriero/projects/parBatch/parSlurm/wip/disBatch.py'
sys.path.append(os.path.dirname(ScriptPath))
import kvsstcp

class BatchContext(object):
    def __init__(self, sysid, jobid, nodes, cylinders, launchFunc, retireFunc):
        # Could make nodes => cylinders a dict since that's how it's used in EngineBlock
        self.sysid, self.jobid, self.nodes, self.cylinders, self.launchFunc, self.retireFunc = sysid, jobid, nodes, cylinders, launchFunc, retireFunc
        self.wd = os.getcwd() #TODO: Easy enough to override, but still... Is this the right place for this?
        self.retiredNodes = set()

    def __str__(self):
        return 'Batch system: %s\nJobID: %s\nNodes: %r\nCylinders: %r\nLaunch function: %s\n'%(self.sysid, self.jobid, self.nodes, self.cylinders, repr(self.launchFunc))

class TaskInfo(object):
    def __init__(self, taskId, taskStreamIndex, taskRepIndex, taskCmd, host = '', pid = 0, returncode = 0, start = 0, end = 0, outbytes = 0, errbytes = 0):
        self.taskId, self.taskStreamIndex, self.taskRepIndex, self.taskCmd, self.host, self.pid, self.returncode, self.start, self.end, self.outbytes, self.errbytes = taskId, taskStreamIndex, taskRepIndex, taskCmd, host, pid, returncode, start, end, outbytes, errbytes

    def __str__(self):
        flags =  [' ', ' ', ' ']
        if self.returncode: flags[0] = 'R'
        if self.outbytes: flags[1] = 'O'
        if self.errbytes: flags[2] = 'E'
        flags = ''.join(flags)
        return '\t'.join([str(x) for x in [flags, self.taskId, self.taskStreamIndex, self.taskRepIndex, self.host, self.pid, self.returncode, self.end - self.start, self.start, self.end, self.outbytes, self.errbytes, repr(self.taskCmd)]])

class BarrierTask(TaskInfo):
    def __init__(self, taskId, taskStreamIndex, taskRepIndex, taskCmd, key):
        super(BarrierTask, self).__init__(taskId, taskStreamIndex, taskRepIndex, taskCmd, myHostname, myPid)
        self.key = key

# Convert nodelist format (slurm specific?) to an expanded list of nodes.
#    nl     => hosts[,nl]
#    hosts  => prefix[\[ranges\]]
#    ranges => range[,ranges]
#    range  => lo[-hi]
# where lo and hi are numbers
def nl2flat(nl):
    flat = []
    prefix = None
    # break up the node list into: non-ranges, ranges, non-ranges, ...
    # commas in the non-ranges separate hosts, the last of which is the prefix for the following ranges.
    for x in re.split(r'(\[.*?\])', nl):
        if x == '': continue
        if x[0] == '[':
            x = x[1:-1]
            for r in x.split(','):
                lo = hi = r
                if '-' in r:
                    lo, hi = r.split('-')
                fmt = '%s%%0%dd'%(prefix, max(len(lo), len(hi)))
                for x in range(int(lo), int(hi)+1): flat.append(fmt%x)
            prefix = None
        else:
            if prefix: flat.append(prefix)
            pp = x.split(',')
            [flat.append(p) for p in pp[:-1] if p]
            prefix = pp[-1]
    if prefix: flat.append(prefix)
    return flat

# Maybe slurm, ssh, etc, could inherit from BatchContext, and provide simpler probe functions to wrap the constructors, to make the shape of *Launch and *Retire clearer.

def slurmContext():
    if 'SLURM_JOBID' not in os.environ: return None

    jobid = os.environ['SLURM_JOBID']
    nodes = nl2flat(os.environ['SLURM_NODELIST'])

    cylinders = []
    for tr in os.environ['SLURM_TASKS_PER_NODE'].split(','):
        m = re.match(r'([^\(]+)(?:\(x([^\)]+)\))?', tr)
        c, m = m.groups()
        if m == None: m = '1'
        cylinders += [int(c)]*int(m)

    return BatchContext('SLURM', jobid, nodes, cylinders, slurmContextLaunch, slurmContextRetire)

def slurmContextLaunch(context, kvsserver):
    kvs = kvsstcp.KVSClient(kvsserver)
    kvs.put('.context', context)
    kvs.close()
    # start one engine per node using the equivalent of:
    # srun -n $SLURM_JOB_NUM_NODES --ntasks-per-node=1 thisScript --engine
    p = SUB.Popen(['srun', '-n', os.environ['SLURM_JOB_NUM_NODES'], '--ntasks-per-node=1', ScriptPath, '--engine', kvsserver])

def slurmContextRetire(context, node):
    if node.startswith(myHostname) or myHostname.startswith(node):
        logger.info('Refusing to retire ("%s", "%s").', node, myHostname)
    else:
        context.retiredNodes.add(node)
        command = ['scontrol', 'update', 'JobId=%s'%context.jobid, 'NodeList=' + ','.join([n for n in context.nodes if n not in context.retiredNodes])]
        logger.info('Retiring node "%s": %s', node, repr(command))
        try:
            SUB.check_call(command)
        except Exception, e:
            logger.warn('Retirement planning needs improvement: %s', repr(e))

#TODO:
def geContext(): return None
def lsfContext(): return None
def pbsContext(): return None

# The ssh context should be generally applicable when all else fails
# (or there is no resource manager).
#
# To use, set the environment variable DISBATCH_SSH_NODELIST. E.g.:
#     DISBATCH_SSH_NODELIST=hostname0:4,hostname1:5
# indicates 4 cylinders (execution entities) should run on hostname0
# and 5 on hostname1.
#
# You can also specify a "job id" via DISBATCH_SSH_JOBID. If you do
# not provide one, one will be created from the PID and epoch time.
def sshContext():
    if 'DISBATCH_SSH_NODELIST' not in os.environ: return None

    jobid = os.environ.get('DISBATCH_SSH_JOBID', '%d_%.6f'%(os.getpid(), time.time()))

    cylinders, nodes = [], []
    for p in os.environ['DISBATCH_SSH_NODELIST'].split(','):
        n, e = p.split(':')
        if n == 'localhost': n = myHostname
        nodes.append(n)
        cylinders.append(int(e))

    return BatchContext('SSH', jobid, nodes, cylinders, sshContextLaunch, sshContextRetire)

def sshContextLaunch(context, kvsserver):
    kvs = kvsstcp.KVSClient(kvsserver)
    kvs.put('.context', context)
    kvs.close()
    for n in context.nodes:
        prefix = ['ssh', n]
        if n == myHostname: prefix = []
        p = SUB.Popen(prefix + [ScriptPath, '--engine', kvsserver], stdout=open('engine_wrap_%s_%s.out'%(context.jobid, n), 'w'), stderr=open('engine_wrap_%s_%s.err'%(context.jobid, n), 'w'))

def sshContextRetire(context, node):
    logger.info('Retiring node "%s": %s', node, 'ToDo: add clean up hook here?')

ContextProbes = [geContext, lsfContext, pbsContext, slurmContext, sshContext]

# When the user specifies a command that will be generating tasks,
# this class wraps the command's execution so we can trigger a
# shutdown if the user's command fails to send an indication that task
# generation is done.
class WatchIt(Thread):
    def __init__(self, command):
        Thread.__init__(self, name='WatchIt')
        self.daemon = True
        self.command = command
        self.p = SUB.Popen(command)
        self.start()

    def run(self):
        self.p.wait()
        logger.info('Task generating command has exited: %s %d.', repr(self.command), self.p.returncode)
        # allow time for a normal shutdown to complete. since this is
        # a daemon thread, its existence won't prevent an exit.
        time.sleep(30)
        # waited long enough, force a shutdown.
        logger.info('Forcing shutdown (was the done task posted?).')
        os._exit(1)

# When the user specifies tasks will be passed through a KVS, this
# class generates an interable that feeds task from the KVS.
class KVSTaskSource(object):
    def __init__(self, kvsserver):
        self.kvs = kvsstcp.KVSClient(kvsserver)
        self.name = self.kvs.get('task source name', False)
        self.taskkey = self.name + ' task'
        self.resultkey = self.name + ' result %d'
        self.donetask = self.name + ' done!'

    def next(self):
        t = self.kvs.get(self.taskkey, False)
        if t == self.donetask:
            self.kvs.close()
            raise StopIteration
        return t

# Given a task source (generating task command lines), parse the lines and
# produce a TaskInfo generator.
def taskGenerator(tasks):
    tsx = 0 # "line number" of current task
    taskCounter = 0 # next taskId
    prefix = suffix = ''

    while 1:
        try:
            t = tasks.next()
            tsx += 1
        except StopIteration:
            # Signals there will be no more tasks.
            logger.info('Read %d tasks.', taskCounter)
            break

        logger.debug('Task: %s', t)

        if t.startswith('#DISBATCH '):
            m = dbprefix.match(t)
            if m:
                prefix = m.group(1)
                continue
            m = dbsuffix.match(t)
            if m:
                suffix = m.group(1)
                continue
            m = dbrepeat.match(t)
            if m:
                repeats, rx, step = int(m.group('repeat')), 0, 1
                g = m.group('start')
                if g: rx = int(g)
                g = m.group('step')
                if g: step = int(g)
                logger.info('Processing repeat: %d %d %d', repeats, rx, step)
                cmd = prefix + (m.group('command') or '') + suffix
                while repeats > 0:
                    yield TaskInfo(taskCounter, tsx, rx, cmd)
                    taskCounter += 1
                    rx += step
                    repeats -= 1
                continue
            m = dbbarrier.match(t)
            if m:
                yield BarrierTask(taskCounter, tsx, 0, t[:-1], m.group(1))
                taskCounter += 1
                continue
            logger.error('Unknown #DISBATCH directive: %s', t)

        if dbcomment.match(t):
            # Comment or empty line, ignore
            continue

        yield TaskInfo(taskCounter, tsx, 0, prefix + t + suffix)
        taskCounter += 1

    logger.info('Processed %d tasks.', taskCounter)

# Main control loop that sends new tasks to the execution engines and
# processes completed ones.
class Feeder(Thread):
    def __init__(self, kvsserver, context, mailFreq, mailTo, tasks, trackResults):

        # Convert the '.finished task' kvs into a simple Queue
        class FinishedTask(Thread):
            def __init__(self):
                Thread.__init__(self, name='FinishedTask')
                self.daemon = True
                self.queue = Queue()
                self.kvs = kvsstcp.KVSClient(kvsserver)
                self.start()

            def run(self):
                while 1:
                    self.queue.put(self.kvs.get('.finished task'))

        Thread.__init__(self, name='Feeder')
        self.daemon = True
        self.context = context
        self.mailFreq = mailFreq
        self.mailTo = mailTo
        self.tasks = tasks
        self.trackResults = trackResults

        self.finished = FinishedTask()
        self.taskGenerator = taskGenerator(tasks)

        self.kvs = kvsstcp.KVSClient(kvsserver)
        self.shutdown = False
        self.start()

    def sendNotification(self, finished, statusfo, statusfolast):
        import smtplib
        from email.mime.text import MIMEText
        statusfo.seek(statusfolast)
        msg = MIMEText('Last %d:\n\n'%self.mailFreq + statusfo.read())
        msg['Subject'] = '%s (%s) has completed %d tasks.'%(self.tasks.name, self.context.jobid, finished)
        msg['From'] = self.mailTo
        msg['To'] = self.mailTo
        s = smtplib.SMTP()
        s.connect()
        s.sendmail([self.mailTo], [self.mailTo], msg.as_string())

    def updateStatus(self, **args):
        # Make changes visible via KVS.
        self.kvs.get('DisBatch status', False)
        self.kvs.put('DisBatch status', repr(args), False)

    def run(self):
        totalSlots = sum(self.context.cylinders)
        eof = False
        active = 0 # number of currently executing (unfinished) tasks (must be <= totalSlots)
        barrier = None # current BarrierTask or None
        failed, finished = 0, 0

        nametasks = os.path.basename(self.tasks.name)
        failures = '%s_%s_failed.txt'%(nametasks, self.context.jobid)
        statusfo = open('%s_%s_status.txt'%(nametasks, self.context.jobid), 'a+')
        statusfolast = statusfo.tell()

        self.kvs.put('.common env', {'DISBATCH_JOBID': str(self.context.jobid), 'DISBATCH_NAMETASKS': nametasks}) #TODO: Add more later?
        self.kvs.put('DisBatch status', '<Starting...>', False)
        while 1:
            logger.info('Feeder loop: %s.', (eof, finished, active, self.shutdown))
            if self.shutdown: break
            self.updateStatus(eof = eof, barrier = barrier, finished = finished, failed = failed, active = active)

            if active:
                # Deal with finished tasks, waiting if necessary
                # Block if we're waiting at a barrier, at end, or there are no free slots
                try:
                    tinfo = self.finished.queue.get(barrier or eof or active >= totalSlots)
                except Empty:
                    tinfo = None
                if tinfo:
                    logger.debug('Finished task: %s', tinfo)
                    if tinfo.taskId == TaskIdOOB:
                        # A finished tasks with id -1 indicates some sort of OOB control message.
                        if tinfo.taskCmd == CmdRetire:
                            self.context.retireFunc(self.context, tinfo.host)
                        else:
                            logger.error('Unrecognized oob task: %(s)', tinfo)
                        continue

                    if self.trackResults: self.kvs.put(self.tasks.resultkey%tinfo.taskId, str(tinfo), False)
                    finished += 1
                    logger.debug('releasing task slot.')
                    active -= 1
                    statusfo.write(str(tinfo)+'\n')
                    statusfo.flush()
                    if self.mailTo and finished%self.mailFreq == 0:
                        try:
                            self.sendNotification(finished, statusfo, statusfolast)
                            statusfolast = statusfo.tell()
                        except Exception, e:
                            logger.warn('Failed to send notification message: "%s". Disabling.', e)
                            self.mailTo = None
                            # Be sure to seek back to EOF to append
                            statusfo.seek(0, 2)
                    if tinfo.returncode:
                        failed += 1
                        with open(failures, 'a') as f:
                            f.write(tinfo.taskCmd+'\n')
                    if barrier and tinfo.taskId == barrier.taskId:
                        # Complete the barrier task itself, exit barrier mode.
                        logger.info('Finished barrier.')
                        # If user specified a KVS key, use it to signal the barrier is done.
                        if barrier.key:
                            logger.info('put %s: %d.', barrier.key, barrier.taskId)
                            self.kvs.put(barrier.key, str(barrier.taskId), False)
                        barrier = None
                    continue
            else:
                # Nothing running
                # See if we've completed a barrier
                if barrier:
                    # Completed all tasks up to, but not including the barrier. Now complete the barrier.
                    logger.info('Finishing barrier.')
                    # activate the barrier
                    active += 1
                    # post a task finished for the current barrier, just like any other task.
                    barrier.end = time.time()
                    self.kvs.put('.finished task', barrier)
                    continue
                if eof:
                    # All done
                    break

            # Request the next task
            try:
                tinfo = self.taskGenerator.next()
            except StopIteration:
                eof = True
                # Post the poison pill. This may trigger retirement of engines.
                self.kvs.put('.task', [TaskIdOOB, -1, -1, CmdPoison])
                continue

            if isinstance(tinfo, BarrierTask):
                # Enter a barrier. We'll exit when all tasks
                # issued to this point have completed.
                barrier = tinfo
                barrier.start = time.time()
                logger.info('Entering barrier (key is %s).', repr(barrier.key))
                continue

            # At this point, we have a task
            active += 1
            tinfo = [tinfo.taskId, tinfo.taskStreamIndex, tinfo.taskRepIndex, tinfo.taskCmd]
            logger.info('Posting task: %r', tinfo)
            self.kvs.put('.task', tinfo)

        statusfo.close()
        self.kvs.close()


# Once we know the nodes participating in the run, we start an engine
# on each node. The engine in turn starts the number of cylinders
# (execution entities) specified for the node (a map of nodes to
# cylinder count is conveyed via the KVS). Each cylinder executes one
# task at a time. The Injector waits for an available cylinder and
# then waits for a task (grabbing a task before a cylinder is ready
# would prevent an idle cylinder of another engine from performing the
# task). The task is passed to the run method via a queue. The run
# method uses queues to feed tasks to the cylinders and accept
# results from them. It tracks work in progress and notes when the
# "poison" task has been received. This indicates no more tasks will
# be coming. At this point, once all work in progress is done, the
# engine can retire.
class EngineBlock(Thread):
    class Cylinder(mpProcess):
        def __init__(self, context, commonEnv, ciq, coq, cylinderId):
            mpProcess.__init__(self, target=self.run)
            self.daemon = True
            self.context, self.commonEnv, self.ciq, self.coq, self.cylinderId = context, commonEnv, ciq, coq, cylinderId
            self.ebProc, self.obProc, self.taskProc = None, None, None
            if self.context.sysid == 'SSH': signal.signal(signal.SIGTERM, self.killTaskSubproc)
            self.start()

        def killTaskSubproc(self, sig, frame):
            logger.info('Cylinder %d killing sub procs.', self.cylinderId)
            for p in [self.ebProc, self.obProc, self.taskProc]:
                if p and p.returncode == None:
                    logger.info('Cylinder %d sending SIGTERM to %d.', self.cylinderId, p.pid)
                    os.killpg(p.pid, signal.SIGTERM)
            logger.info('Cylinder %d exiting on interrupt, %d, %d.', self.cylinderId, self.pid, self.pgid)
            os._exit(0)

        def run(self):
            self.pgid = os.getpgid(0)
            logger.info('Cylinder %d firing, %d, %d.', self.cylinderId, self.pid, self.pgid)
            baseEnv = os.environ.copy()
            baseEnv.update(self.commonEnv)
            while 1:
                taskId, taskStreamIndex, taskRepIndex, taskCmd = self.ciq.get()
                if taskId == TaskIdOOB:
                    logger.info('Cylinder %d stopping.', self.cylinderId)
                    self.coq.put(['done', 'stopped'])
                    break
                t0 = time.time()
                logger.info('Cylinder %d executing %s.', self.cylinderId, repr([taskId, taskStreamIndex, taskRepIndex, taskCmd]))
                baseEnv['DISBATCH_STREAM_INDEX'], baseEnv['DISBATCH_REPEAT_INDEX'], baseEnv['DISBATCH_TASKID'] = str(taskStreamIndex), str(taskRepIndex), str(taskId)
                pfnarg = {}
                if self.context.sysid == 'SSH': pfnarg['preexec_fn'] = os.setsid
                tp = self.taskProc = SUB.Popen(['/bin/bash', '-c', taskCmd], env=baseEnv, stdin=None, stdout=SUB.PIPE, stderr=SUB.PIPE, **pfnarg)
                obp = self.obProc = SUB.Popen(['wc', '-c'], stdin=tp.stdout, stdout=SUB.PIPE, **pfnarg)
                ebp = self.ebProc = SUB.Popen(['wc', '-c'], stdin=tp.stderr, stdout=SUB.PIPE, **pfnarg)
                tp.wait()
                self.ebProc, self.obProc, self.taskProc = None, None, None
                t1 = time.time()
                # should we wait for obp, ebp here?  would it be more efficient/allow more options to capture output in this process?
                ti = TaskInfo(taskId, taskStreamIndex, taskRepIndex, taskCmd, myHostname, tp.pid, tp.returncode, t0, t1, int(obp.stdout.read()), int(ebp.stdout.read()))
                logger.info('Cylinder %s completed: %s', self.cylinderId, ti)
                self.coq.put(['done', ti])

    class Injector(Thread):
        def __init__(self, kvsserver, throttle, fuelline):
            Thread.__init__(self, name='Injector')
            self.daemon = True
            self.kvs = kvsstcp.KVSClient(kvsserver)
            self.throttle = throttle
            self.fuelline = fuelline
            self.start()

        def run(self):
            while 1:
                self.throttle.acquire()
                ti = self.kvs.get('.task')
                logger.debug('Injector got task %s', repr(ti))
                self.fuelline.put(['task', ti])

    def __init__(self, kvsserver, context):
        Thread.__init__(self, name='EngineBlock')
        self.daemon = True
        for n, cylinders in zip(context.nodes, context.cylinders):
            if n.startswith(myHostname) or myHostname.startswith(n): break
        else:
            logger.error('Couldn\'t find %s in "%s", setting cylinder count to 1.', myHostname, context.nodes)
            cylinders = 1

        self.kvs = kvsstcp.KVSClient(kvsserver)
        self.commonEnv = self.kvs.view('.common env')
        # Note we are using the Queue construct from the
        # mulitprocessing module---we need to coordinate between
        # independent processes.
        self.ciq, self.coq, self.throttle = mpQueue(), mpQueue(), BoundedSemaphore(cylinders)
        self.Injector(kvsserver, self.throttle, self.coq)
        self.cylinders = [self.Cylinder(context, self.commonEnv, self.ciq, self.coq, x) for x in range(cylinders)]
        self.start()

    def run(self):
        inFlight, liveCylinders = 0, len(self.cylinders)
        while liveCylinders:
            tag, o = self.coq.get()
            logger.info('Run loop: %d %d %s %s', inFlight, liveCylinders, tag, repr(o))
            if tag == 'done':
                inFlight -= 1
                if o != 'stopped':
                    self.kvs.put('.finished task', o)
                    self.throttle.release()
                else:
                    liveCylinders -= 1
            elif tag == 'task':
                # Is this to "broadcast" this message to other clients?
                if o[0] == TaskIdOOB: self.kvs.put('.task', o)
                self.ciq.put(o)
                inFlight += 1
            else:
                logger.error('Unknown cylinder input tag: "%s" (%s)', tag, repr(o))
        self.kvs.put('.finished task', TaskInfo(TaskIdOOB, -1, -1, CmdRetire))
        self.kvs.close()

# Fail safe: if we lose KVS connectivity (or someone binds the
# ".shutdown" key), we kill the engine.
class Deadman(Thread):
    def __init__(self, kvsserver, engine, main_pid):
        Thread.__init__(self, name='Deadman')
        self.daemon = True
        self.engine = engine
        self.main_pid = main_pid
        self.kvs = kvsstcp.KVSClient(kvsserver)
        self.start()

    def run(self):
        try:
            self.kvs.view('.shutdown')
        except: pass
        self.engine.shutdown = True
        time.sleep(1)
        nap = False
        for c in self.engine.cylinders:
            if c.is_alive():
                nap = True
                try:
                    logger.info('Deadman terminating %d', c.pid)
                    os.kill(c.pid, signal.SIGTERM)
                except Exception, e:
                    logger.info('Deadman ignoring "%s" while terminating %d', e, c.pid)
        if nap:
            logger.info('Deadman saw live processes.')
            time.sleep(5)
            logger.info('Deadman forcing exit.')
            os._exit(0)

def engine(kvsserver, context):
    import random
    # engine makes at least 3(?) connections to kvs -- look into making client support multiple threads?
    time.sleep(random.random()*5.0)
    e = EngineBlock(kvsserver, context)
    d = Deadman(kvsserver, e, myPid)
    e.join()
    logger.info('Engine exiting normally.')

if '__main__' == __name__:
    import argparse

    sys.setcheckinterval(1000000)

    if len(sys.argv) > 1 and sys.argv[1] == '--engine':
        argp = argparse.ArgumentParser(description='Task execution engine.')
        argp.add_argument('--engine', action='store_true', help='Run in execution engine mode.')
        argp.add_argument('kvsserver', help='Address of kvs sever used to relay data to this execution engine.')
        args = argp.parse_args()
        kvs = kvsstcp.KVSClient(args.kvsserver)
        context = kvs.view('.context')
        kvs.close()
        try:
            os.chdir(context.wd)
        except Exception, e:
            print >>sys.stderr, 'Failed to change working directory to "%s".'%context.wd
        logger = logging.getLogger('DisBatch Engine')
        lconf = {'format': '%(asctime)s %(levelname)-8s %(name)-15s: %(message)s', 'level': logging.INFO}
        lconf['filename'] = '%s_%s_%s_engine.log'%('disBatch', context.jobid, myHostname)
        logging.basicConfig(**lconf)
        logger.info('Starting engine (%d) on %s in %s.', myPid, myHostname, os.getcwd())
        engine(args.kvsserver, context)
    else:
        argp = argparse.ArgumentParser(description='Use batch resources to process a file of tasks, one task per line.')
        argp.add_argument('-l', '--logfile', default=None, type=argparse.FileType('w'), help='Log file.')
        argp.add_argument('--mailFreq', default=None, type=int, metavar='N', help='Send email every N task completions (default: 1). "--mailTo" must be given.')
        argp.add_argument('--mailTo', default=None, help='Mail address for task completion notification(s).')
        #argp.add_argument('--tasksPerNode', type=int, help='Maximum concurrently executing tasks per node (default: node core count).')
        argp.add_argument('--web', action='store_true', help='Enable web interface.')
        source = argp.add_mutually_exclusive_group(required=True)
        source.add_argument('--taskcommand', default=None, help='Tasks will come from the command specified via a kvs server instantiated for that purpose.')
        source.add_argument('--taskserver', default=None, help='Tasks will come via the specified kvs server.')
        source.add_argument('taskfile', nargs='?', default=None,  type=argparse.FileType('r'), help='File with tasks, one task per line.')
        args = argp.parse_args()

        if args.mailFreq and not args.mailTo:
            argp.print_help()
            sys.exit(-1)
        if not args.mailFreq and args.mailTo:
            args.mailFreq = 1

        # Try to find a batch context.
        for cf in ContextProbes:
            context = cf()
            if context: break
        else:
            print >>sys.stderr, 'Cannot determine batch execution environment.'
            sys.exit(-1)

        logger = logging.getLogger('DisBatch')
        lconf = {'format': '%(asctime)s %(levelname)-8s %(name)-15s: %(message)s', 'level': logging.INFO}
        if args.logfile:
            args.logfile.close()
            lconf['filename'] = args.logfile.name
        else:
            lconf['filename'] = '%s_%s_log.txt'%('disBatch', context.jobid)
        logging.basicConfig(**lconf)

        logger.info('Starting feeder (%d) on %s in %s.', myPid, myHostname, os.getcwd())
        logger.info('Context: %s', context)

        #TODO: Resist rush to judgment. Could we, for example, want to have tasks from a file, but reporting via kvs?
        if args.taskserver:
            kvsst = None
            kvsserver = args.taskserver
            taskSource = KVSTaskSource(kvsserver)
            trackResults = True
        else:
            kvsst = kvsstcp.KVSServerThread(socket.gethostname(), 0)
            kvsserver = '%s:%d'%kvsst.cinfo
            with open('kvsinfo.txt', 'w') as kvsi:
                kvsi.write(kvsserver)
            if args.taskcommand:
                os.environ['KVSSTCP_HOST'] = kvsst.cinfo[0]
                os.environ['KVSSTCP_PORT'] = str(kvsst.cinfo[1])
                wit = WatchIt(['/bin/bash', '-c', args.taskcommand])
                taskSource = KVSTaskSource(kvsserver)
                trackResults = True
            else:
                trackResults = False
                taskSource = args.taskfile
        nametasks = os.path.basename(taskSource.name)

        logger.info('KVS Server: %s', kvsserver)

        if args.web:
            urlfile = '%s_%s_url'%(nametasks, context.jobid)
            w = SUB.Popen([os.path.dirname(ScriptPath)+'/wskvsmu.py', '--urlfile', urlfile, '-s', ':gpvw', kvsserver], stdout=open('/dev/null', 'w'), stderr=open('/dev/null', 'w'))

        context.launchFunc(context, kvsserver)

        f = Feeder(kvsserver, context, args.mailFreq, args.mailTo, taskSource, trackResults)
        f.join()

        if kvsst: kvsst.server.shutdown()
