# Multithreaded analysis of observation dumps (from toolconf.result_dir)
# Interacts with datamodel
# With respect to SQL database - fills the table 'Injections'
# Renames dumps according to global unique key, stores them into zip package
# Author: Ilya Tuzov, Universitat Politecnica de Valencia

import sys
import xml.etree.ElementTree as ET
import re
import os
import stat
import subprocess
import shutil
import datetime
import time
import random
import glob
import threading
from threading import Thread
from Davos_Generic import *
from Datamanager import *




def process_dumps_in_linst(config, toolconf, conf, datamodel, DescItems, baseindex):
    #model = datamodel.HdlModel_dict[conf.label]
    model = datamodel.GetHdlModel(conf.label)
    basetime = datamodel.reference.reference_dump.vectors[0].time
    ExpDescIdCnt = baseindex
    for item in DescItems:
        if ExpDescIdCnt % 10 == 0: 
            sys.stdout.write("\r%s: Processing dump: %6i" % (conf.label, ExpDescIdCnt) )            
            sys.stdout.flush()
        target = datamodel.GetOrAppendTarget(item.target, item.instance_type, item.injection_case)
        InjDesc = InjectionDescriptor()
        InjDesc.ID = ExpDescIdCnt
        InjDesc.ModelID = model.ID
        InjDesc.TargetID = target.ID
        InjDesc.FaultModel = item.fault_model
        InjDesc.ForcedValue = item.forced_value
        InjDesc.InjectionTime = item.injection_time
        InjDesc.InjectionDuration = item.duration
        InjDesc.ObservationTime = item.observation_time
        inj_dump = simDump()
        inj_dump.set_labels_copy(datamodel.reference.initial_internal_labels, datamodel.reference.initial_output_labels)
        if inj_dump.build_vectors_from_file(os.path.join(conf.work_dir, toolconf.result_dir, item.dumpfile)) == None:
            InjDesc.Status = 'E'    #error
        else:
            InjDesc.Status = 'S'    #Simulted and dumpfile exists
            inj_dump.join_output_columns(datamodel.reference.JnGrLst.copy())
            failure_flag = False
            error_detected = False
            #ANALYSIS            
            #Get vector at finish_time for faulty and reference dumps
            finish_flag = ''
            if config.genconf.finish_flag != '' : 
                finish_flag = config.genconf.finish_flag
            elif toolconf.finish_flag != '':
                finish_flag ='FinishFlag'                
           
            if finish_flag != '':
                fin_vect_ref = datamodel.reference.reference_dump.get_first_vector_by_key(finish_flag, '1')            
                fin_vect_inj = inj_dump.get_first_vector_by_key(finish_flag, '1')
            else:   #if finish flag not present - assume vector at workload finish is the last vector in the dump
                fin_vect_ref = datamodel.reference.reference_dump.vectors[-1]
                fin_vect_inj = inj_dump.get_closest_forward(fin_vect_ref.time)

            if config.analyzer.detect_failures_at_finish_time == True:
                #Check for failures by finish vector and if present, check for recovery
                if(fin_vect_inj == None): #if there is no vector at/after finish time (model hang) - assume failure
                    InjDesc.Status = 'H' #h - hang
                    failure_flag = True
                else:
                    for i in range(0, len(fin_vect_inj.outputs), 1):
                        if(fin_vect_inj.outputs[i] != fin_vect_ref.outputs[i]):
                            failure_flag = True
                            break   
                failure_vector = datamodel.reference.reference_dump.get_first_fail_vector(inj_dump, config.analyzer.neg_timegap, config.analyzer.pos_timegap, float(0), basetime + float(InjDesc.InjectionTime))
            else:
                #check for failures by comparing outputs at all timepoint during workload execution
                failure_vector = datamodel.reference.reference_dump.get_first_fail_vector(inj_dump, config.analyzer.neg_timegap, config.analyzer.pos_timegap, config.analyzer.check_duration_factor, basetime + float(InjDesc.InjectionTime))
                if failure_vector != None:
                    failure_flag = True

            InjDesc.FaultToFailureLatency = float(0)                            
            if failure_flag:
                if config.analyzer.error_flag_signal != '':
                    if inj_dump.get_forward_by_key(basetime + float(InjDesc.InjectionTime), config.analyzer.error_flag_signal, config.analyzer.error_flag_active_value) != None:
                        error_detected = True
                        InjDesc.TrapCode = inj_dump.get_value_where(config.analyzer.trap_type_signal, config.analyzer.error_flag_signal, config.analyzer.error_flag_active_value)
                #compute fault to failure latency
                if failure_vector != None:
                     InjDesc.FaultToFailureLatency = failure_vector.time- basetime - float(InjDesc.InjectionTime)
                     if InjDesc.FaultToFailureLatency < 0:  InjDesc.FaultToFailureLatency = float(0)

            InjDesc.ErrorCount = 0
            #Compute number of errors comparing vector of internals at finish_time inj==ref
            if fin_vect_inj != None:
                for i in range(0, len(fin_vect_inj.internals), 1):
                    if(fin_vect_inj.internals[i] != fin_vect_ref.internals[i]):
                        InjDesc.ErrorCount += 1

            #Determine failure mode                    
            if not failure_flag:
                if InjDesc.ErrorCount == 0:
                    InjDesc.FailureMode = 'M'   #Masked fault
                else:
                    InjDesc.FailureMode = 'L'   #Latent fault
            else:
                if error_detected:
                    InjDesc.FailureMode = 'S'   #Signaled Failure
                else:
                    InjDesc.FailureMode = 'C'   #Silent Data Corruption
        #rename dumpfile to string of unique index {InjDesc.ID}.lst
        InjDesc.Dumpfile = '{0:010d}.lst'.format(InjDesc.ID)
        src = os.path.normpath(os.path.join(conf.work_dir, toolconf.result_dir, item.dumpfile))        
        dst = os.path.normpath(os.path.join(conf.work_dir, 'irespack',          InjDesc.Dumpfile))
        if os.path.exists(src): shutil.copy(src,  dst)        
        datamodel.LaunchedInjExp_dict[InjDesc.ID] = InjDesc
        ExpDescIdCnt += 1


def process_dumps(config, toolconf, conf, datamodel):
    timestart = datetime.datetime.now().replace(microsecond=0)
    os.chdir(conf.work_dir)
    packdir = os.path.join(conf.work_dir, 'irespack')
    if os.path.exists(packdir): shutil.rmtree(packdir)
    os.mkdir(packdir)
    shutil.copy( os.path.normpath( os.path.join(conf.work_dir, toolconf.result_dir, toolconf.reference_file)) , os.path.normpath(os.path.join(packdir, toolconf.reference_file)) )
    datamodel.reference.reference_dump = simDump()
    datamodel.reference.reference_dump.build_labels_from_file(os.path.normpath(os.path.join(conf.work_dir, toolconf.list_init_file)), config.analyzer.rename_list)
    datamodel.reference.reference_dump.normalize_array_labels(os.path.normpath(os.path.join(conf.work_dir, toolconf.result_dir, toolconf.reference_file)))
    datamodel.reference.reference_dump.build_vectors_from_file(os.path.normpath(os.path.join(conf.work_dir, toolconf.result_dir, toolconf.reference_file)))
    datamodel.reference.initial_internal_labels, datamodel.reference.initial_output_labels = datamodel.reference.reference_dump.get_labels_copy()
    datamodel.reference.JnGrLst = config.analyzer.join_group_list.copy()
    datamodel.reference.reference_dump.join_output_columns(datamodel.reference.JnGrLst.copy())
    desctable = ExpDescTable(conf.label)
    desctable.build_from_csv_file(os.path.normpath(os.path.join(conf.work_dir, toolconf.result_dir, toolconf.exp_desc_file)), "Other")
    #desctable.normalize_targets()        

    #Append all targets in main thread, so assigned Target.ID (primary key) will be the same for any tool run (with any number of threads)
    for i in desctable.items:
        target = datamodel.GetOrAppendTarget(i.target, i.instance_type, i.injection_case)
    #Prepare multithreaded analysis of dumps
    ExpDescIdCnt = datamodel.GetMaxKey(DataDescriptors.InjectionExp) + 1    
    threadlist = []
    step = (len(desctable.items) / config.analyzer.threads) + 1
    index = 0
    while(index < len(desctable.items)):
        if index + step <= len(desctable.items):
            items = desctable.items[index:index+step]
        else:
            items = desctable.items[index:]             
        baseindex = ExpDescIdCnt + index
        t = Thread(target = process_dumps_in_linst, args = (config, toolconf, conf, datamodel, items, baseindex))
        threadlist.append(t)
        index += step
    for t in threadlist:
        t.start()
    for t in threadlist:
        t.join()

    datamodel.SaveTargets()
    datamodel.SaveInjections()

    dumppack = "RESPACK_{0}.zip".format(conf.label)
    arc_script = toolconf.archive_tool_script + " " + dumppack + " " + "irespack"   +  " " + toolconf.list_init_file  + " > ziplog.log"
    os.chdir(conf.work_dir)
    print "Compressing results: " + arc_script
    proc = subprocess.Popen(arc_script, shell=True)
    ziptime = 0
    while proc.poll() is None:
        sys.stdout.write( "\rzip in process: {0} seconds".format(str(ziptime)) )            
        sys.stdout.flush()        
        time.sleep(5)
        ziptime += 5
    shutil.rmtree(packdir)
    if os.path.exists(os.path.join(conf.work_dir, dumppack)):
        shutil.move(os.path.join(conf.work_dir, dumppack), os.path.join(config.report_dir, dumppack))
        
    datamodel.LaunchedInjExp_dict.clear()    
    print('\n\nAnalysys completed, time taken: ' + str(time_to_seconds(datetime.datetime.now().replace(microsecond=0) - timestart)))
