import sys
import xml.etree.ElementTree as ET
import re
import os
import datetime
import subprocess
import shutil
import string
import copy
import time
import glob
import random
import multiprocessing
import copy
from subprocess import call
from sys import platform
from Datamanager import *
import ImplementationTool
from XilinxInjectorHost import *


class EvalEngineParameters:
    FIT_DEVICE = (75.0/float(1024*1024))
    requires_properties_implement = ['FREQUENCY', 'POWER_DYNAMIC', 'POWER_PL', 'UTIL_FF', 'UTIL_LUT', 'UTIL_BRAM', 'UTIL_DSP']
    require_properties_faulteval = ['VerificationSuccess', 'Injections', 'EssentialBits', 'Failures', 'FailureRate', 'FailureRateMargin', 'FIT', 'FITMargin', 'Lambda', 'MTTF']
    #require_properties_faulteval = ['VerificationSuccess', 'Injections', 'EssentialBits', 'Failures', 'FailureRate', 'FailureRateMargin', 'FIT', 'Lambda', 'MTTF']
    require_properties = requires_properties_implement + require_properties_faulteval




def invalidate_robustness_metrics(configurations):
    for c in configurations:
        if 'Implprop' in c.Metrics:
            c.Metrics['Implprop']['VerificationSuccess']=0
            c.Metrics['Implprop']['Injections']=0
            c.Metrics['Implprop']['EssentialBits']=0
            c.Metrics['Implprop']['Failures']=0
            c.Metrics['Implprop']['FailureRate']=0.0
            c.Metrics['Implprop']['FailureRateMargin']=0.0
            c.Metrics['Implprop']['FIT']=0.0
            c.Metrics['Implprop']['FITMargin']=0.0
            c.Metrics['Implprop']['Lambda']=0.0
            c.Metrics['Implprop']['MTTF']=0.0
            c.Metrics['EvalTime']['RobustnessAssessment'] = 0
            c.Metrics['SampleSizeGoal'] =10000
            c.Metrics['ErrorMarginGoal'] = 0.0
            


class JobManager:
    def __init__(self, proc_num):
        self.proc_num = proc_num
        self.DeviceList =   [{'TargetId':'2', 'PortID':'COM3'},{'TargetId':'6', 'PortID':'COM5'}] #[]
        if len(self.DeviceList) == 0:
            self.DeviceList = get_devices('Cortex-A9 MPCore #0')       
        if raw_input('Clean the cache before running: Y/N: ').lower().startswith('y'):                                   
            for i in self.DeviceList:   #cleanup (cache, ...) each platform
                cleanup_platform(i['TargetId'], i['PortID'])
        self.manager = multiprocessing.Manager()
        self.console_lock = self.manager.Lock()
        self.queue_implement = self.manager.Queue()
        self.queue_faulteval = self.manager.Queue()
        self.queue_result = self.manager.Queue()
        self.ids_imp = self.manager.Queue()
        for i in range(proc_num): self.ids_imp.put(i)
        self.ids_inj = self.manager.Queue()
        for i in range(len(self.DeviceList)): self.ids_inj.put(i)                       
        implement_Pool = multiprocessing.Pool(proc_num, worker_Implement,(self.ids_imp, self.queue_implement, self.queue_faulteval, self.console_lock))
        faulteval_Pool = multiprocessing.Pool(len(self.DeviceList), worker_Faulteval,(self.ids_inj, self.queue_faulteval, self.queue_result, self.console_lock, self.DeviceList))    
        



def worker_Implement(idx, queue_i, queue_o, lock):
    id_proc = idx.get()
    while True:
        item = queue_i.get(True)
        model = item[0]
        stat = item[1]
        config = item[2]
        with lock: print('worker_Implement {0} :: Implementing {1}'.format(id_proc, model.Label))           
          
        ImplementationTool.implement_model(config, model, True, stat, False)
        #dummy_implement(config, model, True, stat)

        model.Metrics['Error'] = ''
        for i in EvalEngineParameters.requires_properties_implement:
            if not i in model.Metrics['Implprop']:
                model.Metrics['Implprop'][i] = 0
                model.Metrics['Error'] = 'ImplementError'
        queue_o.put(item)





def worker_Faulteval(idx, queue_i, queue_o, lock, DeviceList):
    id_proc = idx.get()
    while True:
        item = queue_i.get(True)
        model = item[0]
        stat = item[1]
        config = item[2]
        if model.Metrics['Error'] != '':
            with lock: print('worker_Faulteval {0} :: Passing {1} due to error flag'.format(id_proc, model.Label))     
        else:
            with lock: print('worker_Faulteval {0} :: Estimating Robustness {1}'.format(id_proc, model.Label))     

            estimate_robustness(model, DeviceList[id_proc], stat, config, lock)
            #dummy_estimate_robustness(model, id_proc, stat)


        for i in EvalEngineParameters.require_properties_faulteval:
            if not i in model.Metrics['Implprop']:
                model.Metrics['Implprop'][i] = 0
                if model.Metrics['Error'] == '':
                    model.Metrics['Error'] = 'InjError'
        queue_o.put(item)




def estimate_robustness(model, Device, stat, config, lock):
    os.chdir(model.ModelPath)
    if stat == None: stat = ProcStatus('Config')
    for k, v in model.Metrics['Implprop'].iteritems():
        stat.update(k, str(v), 'res')
    stat.update('Progress', 'RobustnessAssessment', '0%')
    stat.update('RobustnessAssessment', 'In progress', 'wait')
    if not 'Implprop' in model.Metrics: model.Metrics['Implprop'] = dict()
    for k,v in model.Metrics['EvalTime'].iteritems():
        stat.update(str(k), '{} sec'.format(v), 'ok')

    Injector = InjectorHostManager(config.call_dir, model.ModelPath, model.ID)       

    with lock: print('Evaluating configuration {} on device {}\n\nMetrics: {} \n\n\n'.format(model.ID, str(Device), str(model.Metrics)))

    Injector.attach_device(Device['TargetId'], Device['PortID'])
    if config.design_genconf.post_injection_recovery_nodes != None:
        for i in config.design_genconf.post_injection_recovery_nodes:
            Injector.RecoveryNodeNames.append(i)
    check = Injector.check_fix_preconditions()
    if not check: 
        model.Metrics['Implprop']['VerificationSuccess'] = int(0)
        stat.update('Progress', 'RobustnessAssessment', 'error')
        stat.update('RobustnessAssessment', 'error', 'err')
        return

    #Setup Job Parameters
    jdesc = JobDescriptor(model.ID)
    if 'SampleSizeGoal' in model.Metrics and model.Metrics['SampleSizeGoal'] > 0:
        jdesc.Mode = 2
        jdesc.sample_size_goal = int(model.Metrics['SampleSizeGoal'])
        opmode = OperatingModes.SampleExtend
    elif 'ErrorMarginGoal' in model.Metrics and model.Metrics['ErrorMarginGoal'] > 0:
        jdesc.Mode = 2
        jdesc.error_margin_goal = float(model.Metrics['ErrorMarginGoal'])
        opmode = OperatingModes.SampleUntilErrorMargin
    else:
        jdesc.Mode = 3
        opmode = OperatingModes.Exhaustive

    with lock: print('\nMODE SELECTED: {}, \n'.format(str(opmode)))

    print('\n\nMetrics before injection: {}\n\n'.format(str(model.Metrics['Implprop'])))

    jdesc.UpdateBitstream = 1
    if 'Injections' in model.Metrics['Implprop']:
        if int(model.Metrics['Implprop']['Injections']) > 0:
            jdesc.UpdateBitstream = 0
           
    jdesc.Blocktype = 0         #CLB only
    jdesc.Essential_bits = 1    
    jdesc.CheckRecovery = 10    #check recovery after 10 experiments
    jdesc.LogTimeout = 500
    if 'Injections' in model.Metrics['Implprop']: 
        jdesc.StartIndex =    int(model.Metrics['Implprop']['Injections'])
        jdesc.ExperimentsCompleted =    int(model.Metrics['Implprop']['Injections'])
    if jdesc.StartIndex > 0:
        if 'Failures'   in model.Metrics['Implprop']: jdesc.Failures =      int(model.Metrics['Implprop']['Failures'])
        if 'Masked'     in model.Metrics['Implprop']: jdesc.Masked =        int(model.Metrics['Implprop']['Masked'])
        
    timestart = datetime.datetime.now().replace(microsecond=0)
    res = Injector.run(opmode, jdesc, True)
    with lock: print('Got Injection Result: {}'.format(model.ID))

    if res:
        #if not 'Implprop' in model.Metrics: model.Metrics['Implprop'] = dict()
        model.Metrics['Implprop']['VerificationSuccess'] = int(1) if res.VerificationSuccess else int(0)
        model.Metrics['Implprop']['InjectorError'] = int(1) if res.InjectorError else int(0)
        model.Metrics['Implprop']['Injections'] = int(res.ExperimentsCompleted)
        model.Metrics['Implprop']['Failures'] = int(res.Failures)
        model.Metrics['Implprop']['FailureRate'] = float(res.failure_rate)
        model.Metrics['Implprop']['FailureRateMargin'] = float(res.failure_error)
        model.Metrics['Implprop']['Masked'] = int(res.Masked)
        model.Metrics['Implprop']['MaskedRate'] = float(res.masked_rate)
        model.Metrics['Implprop']['MaskedRateMargin'] = float(res.masked_error)
        model.Metrics['Implprop']['EssentialBits'] = int(res.EssentialBitsCount)
        model.Metrics['Implprop']['FIT'] = EvalEngineParameters.FIT_DEVICE * model.Metrics['Implprop']['EssentialBits'] * (model.Metrics['Implprop']['FailureRate'] / 100.0)
        model.Metrics['Implprop']['FITMargin'] = EvalEngineParameters.FIT_DEVICE * model.Metrics['Implprop']['EssentialBits'] * (model.Metrics['Implprop']['FailureRateMargin'] / 100.0)
        model.Metrics['Implprop']['Lambda'] = model.Metrics['Implprop']['FIT']/float(1000000000)
        model.Metrics['Implprop']['MTTF'] = 1.0 / model.Metrics['Implprop']['Lambda']
    #timetaken = str(datetime.datetime.now().replace(microsecond=0) - timestart)        
    for k, v in model.Metrics['Implprop'].iteritems():
        stat.update(k, str(v), 'res')
    stat.update('RobustnessAssessment', 'Completed', 'ok')
    Injector.cleanup()
    timetaken = datetime.datetime.now().replace(microsecond=0) - timestart
    stat.update('Progress', 'Completed', 'ok')
    if not 'EvalTime' in model.Metrics: model.Metrics['EvalTime'] = dict()
    if not 'RobustnessAssessment' in model.Metrics['EvalTime']: 
        model.Metrics['EvalTime']['RobustnessAssessment'] = int(time_to_seconds(timetaken))
        if res.Time != None: model.Metrics['EvalTime']['RobustnessAssessment'] += int(res.Time)
    else: 
        model.Metrics['EvalTime']['RobustnessAssessment'] += int(time_to_seconds(timetaken))
    stat.update('RobustnessAssessment', '{} sec'.format(model.Metrics['EvalTime']['RobustnessAssessment']), 'ok')
    with lock: print('Finished estimate_robustness for : {}'.format(model.ID))






def dummy_implement(config, model, adjust_constraints, stat):
    stat.update('Progress', 'Implement', '0%')
    stat.update('Implement', 'In progress', 'wait')
    time.sleep(1) # simulate a "long" operation
    if not 'Implprop' in model.Metrics: model.Metrics['Implprop'] = dict()
    model.Metrics['Implprop']['FREQUENCY'] = 100.1
    model.Metrics['Implprop']['LUT'] = 500
    model.Metrics['Implprop']['FF'] = 400
    for k, v in model.Metrics['Implprop'].iteritems():
        stat.update(k, str(v), 'res')
    stat.update('Implement', 'Completed', 'ok')


def dummy_estimate_robustness(model, Device, stat):
    time.sleep(1) # simulate a "long" operation
    model.Metrics['Implprop']['VerificationSuccess'] = 1
    model.Metrics['Implprop']['Injections'] = 1000
    model.Metrics['Implprop']['Failures'] = 500
    model.Metrics['Implprop']['FailureRate'] = 5.01
    model.Metrics['Implprop']['FIT'] = 3.14
    model.Metrics['Implprop']['Lambda'] = 1.1
    model.Metrics['Implprop']['MTTF'] = 100000.1




def allocate_gui_global(config):
    returndir = os.getcwd()
    os.chdir(config.design_genconf.design_dir)
    copy_all_files(os.path.join(config.call_dir,'UserInterface/IMPL'), config.design_genconf.design_dir)
    copy_all_files(os.path.join(config.call_dir,'UserInterface/libs'), os.path.join(config.design_genconf.design_dir, 'libs'))
    with open(os.path.join(config.design_genconf.design_dir, 'Monitoring.js'), 'r') as f:
        content = f.read()
    content = content.replace('#FULLSTAT', '\'{0}\''.format(config.statfile)).replace('#MINSTAT', '\'{0}\''.format(config.statfile.replace('.xml','_min.xml')))
    theaders = ['Label']
    tlogdesc = ['./Logs/@.log']
    for c in EvalEngineParameters.require_properties:
        theaders.append(c)
        tlogdesc.append('./Logs/@.log')
    theaders.append('Progress')
    tlogdesc.append('./Logs/@.log')
    theaders.append('Iteration')
    tlogdesc.append('./Logs/@.log')
    for p in config.flow.get_phase_chain():
        theaders.append(p.name)
        tlogdesc.append('./@/{0}/{1}'.format(config.design_genconf.log_dir, p.logfile))
    theaders.append('RobustnessAssessment')
    tlogdesc.append('./@/{0}/{1}'.format(config.design_genconf.log_dir, 'Injector.log'))
    content = content.replace('#THEADERS', '[{0}]'.format(', '.join(['\'{0}\''.format(c) for c in theaders])))
    content = content.replace('#LOGDESC', '[{0}]'.format(', '.join(['\'{0}\''.format(c)  for c in tlogdesc])))
    content = content.replace('#GLOBALHEADERS', "['Phase', 'Progress', 'Time_Taken', 'Report']")
    with open(os.path.join(config.design_genconf.design_dir, 'Monitoring.js'), 'w') as f:
        f.write(content)
    #Launch monitoring interface
    try:
        if platform == 'linux' or platform == 'linux2':
             subprocess.check_output('xdg-open ' + os.path.join(config.call_dir, config.design_genconf.design_dir, 'Monitoring.html > ./dummylog.txt'), shell=True)
        elif platform == 'cygwin': 
            subprocess.check_output('cygstart ' + os.path.join(config.call_dir, config.design_genconf.design_dir, 'Monitoring.html > ./dummylog.txt'), shell=True)
        elif platform == 'win32' or  platform == 'win64':
            subprocess.check_output('start ' + os.path.join(config.call_dir, config.design_genconf.design_dir, 'Monitoring.html > ./dummylog.txt'), shell=True)
    except subprocess.CalledProcessError as e:
        print e.output
    os.chdir(returndir)


def export_DatamodelStatistics(datamodel, fname):
    res = '<?xml version=\"1.0\"?>\n<data>'
    for m in datamodel.HdlModel_lst:
        res += "\n\n<Config\n\tLabel = \"{}\"".format(m.Label)
        if 'Implprop' in m.Metrics and isinstance(m.Metrics['Implprop'], dict):
            for k,v in m.Metrics['Implprop'].iteritems():
                res += "\n\t{} = \"{}$res\"".format(str(k), str(v))
        validmark = 'ok' if 'Error' in m.Metrics and m.Metrics['Error'] == "" else 'err'
        if 'EvalTime' in m.Metrics and isinstance(m.Metrics['EvalTime'], dict):
            for k,v in m.Metrics['EvalTime'].iteritems():
                res += "\n\t{} = \"{}${}\"".format(str(k), str(v), validmark)        
        res += '\n/>'
    res += '\n</data>'
    with open(fname, 'w') as f:
        f.write(res)


def evaluate(config_list, config, JM, datamodel, force_evaluate = False):
    configurations_to_implement = []   
    configurations_to_inject = []   
    configurations_implemented = []
    for individual in config_list:
        #Check availability of all metrics, required for score computation
        impl_complete = True
        faulteval_complete = True
        if (not 'Implprop' in individual.Metrics) or (not isinstance(individual.Metrics['Implprop'], dict)):
            impl_complete = False
            faulteval_complete = False
        else:
            for p in EvalEngineParameters.requires_properties_implement:
                if not p in individual.Metrics['Implprop']:
                    impl_complete = False
                    break
            for p in EvalEngineParameters.require_properties_faulteval:
                if not p in individual.Metrics['Implprop']:                    
                    faulteval_complete = False
                    break
            if faulteval_complete:
                if ('SampleSizeGoal' in individual.Metrics)  and (individual.Metrics['SampleSizeGoal'] > 0)  and (individual.Metrics['Implprop']['Injections'] < individual.Metrics['SampleSizeGoal']):         faulteval_complete = False
                if ('ErrorMarginGoal' in individual.Metrics) and (individual.Metrics['ErrorMarginGoal'] > 0) and (individual.Metrics['Implprop']['FailureRateMargin'] > individual.Metrics['ErrorMarginGoal']): faulteval_complete = False
        if (not impl_complete) and ('Error' in individual.Metrics) and (individual.Metrics['Error'] != ''):      impl_complete = True
        if (not faulteval_complete) and ('Error' in individual.Metrics) and (individual.Metrics['Error'] != ''): faulteval_complete = True
        if (not impl_complete) or force_evaluate:
            configurations_to_implement.append(individual)
        elif (not faulteval_complete) or force_evaluate:
            configurations_to_inject.append(individual)
        else:
            configurations_implemented.append(individual)            
    #Implement all inviduals with missing metrics
    allocate_gui_global(config)
    buf = ImplementationTool.ImplementConfigurationsManaged(configurations_to_implement, configurations_to_inject, config, JM, datamodel.SaveHdlModels(), 100)
    res = []
    for i in buf:
        m = datamodel.GetHdlModel(i.Label)
        m.Metrics = i.Metrics
        res.append(m)
    individuals = configurations_implemented + res
    #compute the fitness of each individual and rank them (sort descending - first are better)
    #example - minimize failures
    for i in individuals: 
        ComputeScore(i)
        #if i.Metrics['Error'] == '' and i.Metrics['Implprop']['VerificationSuccess'] > 0:
        #    if float(i.Metrics['Implprop']['FREQUENCY']) >= 20.0 and i.Metrics['Implprop']['FIT'] > 0:
        #        i.Metrics['Score'] = 1.0/float(i.Metrics['Implprop']['FIT'])
        #    else:
        #        i.Metrics['Score'] = 0.0
        #else:
        #    i.Metrics['Score'] = 0.0
    return(individuals)


def ComputeScore(ind):
    if ind.Metrics['Error'] == '' and ind.Metrics['Implprop']['VerificationSuccess'] > 0 and ind.Metrics['Implprop']['FIT'] > 0:
        ind.Metrics['Implprop']['FITMargin'] = EvalEngineParameters.FIT_DEVICE * ind.Metrics['Implprop']['EssentialBits'] * (ind.Metrics['Implprop']['FailureRateMargin'] / 100.0)
        ind.Metrics['ScoreMean'] = 1.0/(ind.Metrics['Implprop']['FIT'])
        ind.Metrics['ScoreLow'] =  1.0/(ind.Metrics['Implprop']['FIT']+ind.Metrics['Implprop']['FITMargin'])
        ind.Metrics['ScoreHigh'] = 1.0/(ind.Metrics['Implprop']['FIT']-ind.Metrics['Implprop']['FITMargin'])
    else:
        ind.Metrics['ScoreMean'], ind.Metrics['ScoreLow'], ind.Metrics['ScoreHigh'] = 0.0, 0.0, 0.0


