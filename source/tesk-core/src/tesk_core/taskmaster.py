#!/usr/bin/env python3

import argparse
import json
import os
import re
import sys
import logging
import gzip
from kubernetes import client, config
from tesk_core.job import Job
from tesk_core.pvc import PVC
from tesk_core.filer_class import Filer

created_jobs = []
poll_interval = 5
task_volume_basename = 'task-volume'
args = None
logger = None

def run_executor(executor, namespace, pvc=None, task_data=None):
    jobname = executor['metadata']['name']
    spec = executor['spec']['template']['spec']

    if os.environ.get('EXECUTOR_BACKOFF_LIMIT') is not None:
        executor['spec'].update({'backoffLimit': int(os.environ['EXECUTOR_BACKOFF_LIMIT'])})

    # Simple path alignment: no remapping needed when paths match
    shared_pvc_name = os.environ.get('TRANSFER_PVC_NAME')
    if shared_pvc_name:
        logger.info(f'Using shared PVC: {shared_pvc_name} with direct path mounting')

    if pvc is not None:
        mounts = spec['containers'][0].setdefault('volumeMounts', [])
        mounts.extend(pvc.volume_mounts)
        
        # When using shared PVC, extract unique paths dynamically from JSON
        if shared_pvc_name and task_data:
            unique_paths = set()
            
            # Get base path from environment or use default
            base_path = os.environ.get('TES_BASE_PATH', '/data')
            
            # Extract all paths from inputs URLs
            for input_item in task_data.get('inputs', []):
                if 'url' in input_item:
                    url_path = os.path.dirname(input_item['url'])
                    if url_path and url_path != '/':
                        unique_paths.add(url_path)
            
            # Extract all paths from outputs URLs  
            for output_item in task_data.get('outputs', []):
                if 'url' in output_item:
                    url_path = os.path.dirname(output_item['url'])
                    if url_path and url_path != '/':
                        unique_paths.add(url_path)
            
            # Extract all paths from inputs PATHs
            for input_item in task_data.get('inputs', []):
                if 'path' in input_item:
                    path_dir = os.path.dirname(input_item['path'])
                    if path_dir and path_dir != '/':
                        unique_paths.add(path_dir)
            
            # Extract all paths from outputs PATHs
            for output_item in task_data.get('outputs', []):
                if 'path' in output_item:
                    path_dir = os.path.dirname(output_item['path'])
                    if path_dir and path_dir != '/':
                        unique_paths.add(path_dir)
            
            # Get existing mount paths to avoid duplicates
            existing_mounts = set(mount.get('mountPath', '') for mount in mounts)
            
            # Create dynamic mounts for each unique path, avoiding duplicates
            for mount_path in unique_paths:
                if mount_path != base_path and mount_path not in existing_mounts:  # Skip if already mounted
                    # Calculate subpath by removing base_path prefix
                    if mount_path.startswith(base_path + '/'):
                        subpath = mount_path[len(base_path + '/'):]
                    elif mount_path.startswith(base_path):
                        subpath = mount_path[len(base_path):].lstrip('/')
                    else:
                        subpath = mount_path.lstrip('/')
                    
                    if subpath:  # Only add if subpath is not empty
                        mounts.append({
                            'name': task_volume_basename,
                            'mountPath': mount_path,
                            'subPath': subpath
                        })
                        logger.info(f'Added dynamic mount: {mount_path} -> pvc/{subpath}')
                else:
                    logger.info(f'Skipped duplicate mount: {mount_path} (already exists)')
        
        volumes = spec.setdefault('volumes', [])
        volumes.extend([{'name': task_volume_basename, 'persistentVolumeClaim': {
            'readonly': False, 'claimName': pvc.name}}])
    logger.info('Created job: ' + jobname)
    job = Job(executor, jobname, namespace)
    logger.debug('Job spec: ' + str(job.body))

    global created_jobs
    created_jobs.append(job)

    status = job.run_to_completion(poll_interval, check_cancelled,args.pod_timeout)
    if status != 'Complete':
        if status == 'Error':
            job.delete()
        exit_cancelled('Got status ' + status)

# TODO move this code to PVC class


def append_mount(volume_mounts, name, path, pvc):
    # Checks all mount paths in volume_mounts if the path given is already in
    # there
    duplicate = next(
        (mount for mount in volume_mounts if mount['mountPath'] == path),
        None)
    # If not, add mount path
    if duplicate is None:
        # For shared PVC, calculate actual relative path instead of generated subpath
        shared_pvc_name = os.environ.get('TRANSFER_PVC_NAME')
        if shared_pvc_name and pvc.name == shared_pvc_name:
            # Calculate subpath by removing base_path prefix for shared PVC
            base_path = os.environ.get('TES_BASE_PATH', '/data')
            if path.startswith(base_path + '/'):
                subpath = path[len(base_path + '/'):]
            elif path.startswith(base_path):
                subpath = path[len(base_path):].lstrip('/')
            else:
                subpath = path.lstrip('/')
        else:
            # Original behavior for task-specific PVCs
            subpath = pvc.get_subpath()
            
        logger.debug(' '.join(
            ['appending' + name +
             'at path' + path +
             'with subPath:' + subpath]))
        volume_mounts.append(
            {'name': name, 'mountPath': path, 'subPath': subpath})


def dirname(iodata):
    if iodata['type'] == 'FILE':
        # strip filename from path
        r = '(.*)/'
        dirname = re.match(r, iodata['path']).group(1)
        logger.debug('dirname of ' + iodata['path'] + 'is: ' + dirname)
    elif iodata['type'] == 'DIRECTORY':
        dirname = iodata['path']

    return dirname


def generate_mounts(data, pvc):
    volume_mounts = []

    # gather volumes that need to be mounted, without duplicates
    volume_name = task_volume_basename
    for volume in data['volumes']:
        mount_path = volume
        append_mount(volume_mounts, volume_name, mount_path, pvc)

    # gather other paths that need to be mounted from inputs/outputs FILE and
    # DIRECTORY entries  
    for aninput in data['inputs']:
        dirnm = dirname(aninput)
        append_mount(volume_mounts, volume_name, dirnm, pvc)

    for anoutput in data['outputs']:
        dirnm = dirname(anoutput)
        append_mount(volume_mounts, volume_name, dirnm, pvc)

    return volume_mounts


def init_pvc(data, filer):
    global created_pvc  # Declare global variable at the beginning
    task_name = data['executors'][0]['metadata']['labels']['taskmaster-name']
    
    # Check if we should use shared PVC instead of creating task-specific PVC
    shared_pvc_name = os.environ.get('TRANSFER_PVC_NAME')
    if shared_pvc_name:
        logger.info(f'Using shared PVC: {shared_pvc_name} for direct mounting')
        
        # Create a PVC object that references the existing shared PVC
        pvc = PVC(shared_pvc_name, 0, args.namespace)  # size 0 means don't create
        
        #mounts = generate_mounts(data, pvc) avoid generating incompatible mounts for shared PVC 
        logging.debug(mounts)
        logging.debug(type(mounts))
        pvc.set_volume_mounts(mounts)
        
        # Only add volume mount to filer if filer is provided
        if filer:
            filer.add_volume_mount(pvc)
        
        # Skip PVC creation and filer jobs - use direct mounting
        created_pvc = None  # Don't track for cleanup since we didn't create it
        
        if os.environ.get('NETRC_SECRET_NAME') is not None and filer:
            filer.add_netrc_mount(os.environ.get('NETRC_SECRET_NAME'))
            
        logger.info('Skipping filer jobs - using direct PVC mounting')
        return pvc
    else:
        # Original behavior for backwards compatibility
        pvc_name = task_name + '-pvc'
        pvc_size = data['resources']['disk_gb']
        pvc = PVC(pvc_name, pvc_size, args.namespace)

        mounts = generate_mounts(data, pvc)
        logging.debug(mounts)
        logging.debug(type(mounts))
        pvc.set_volume_mounts(mounts)
        filer.add_volume_mount(pvc)

        pvc.create()
        # to global var for cleanup purposes
        created_pvc = pvc

        if os.environ.get('NETRC_SECRET_NAME') is not None:
            filer.add_netrc_mount(os.environ.get('NETRC_SECRET_NAME'))

        filerjob = Job(
            filer.get_spec('inputs', args.debug),
            task_name + '-inputs-filer',
            args.namespace)

        global created_jobs
        created_jobs.append(filerjob)
        # filerjob.run_to_completion(poll_interval)
        status = filerjob.run_to_completion(poll_interval, check_cancelled, args.pod_timeout)
        if status != 'Complete':
            exit_cancelled('Got status ' + status)

        return pvc


def run_task(data, filer_name, filer_version, have_json_pvc=False):
    task_name = data['executors'][0]['metadata']['labels']['taskmaster-name']
    pvc = None

    if have_json_pvc:
        json_pvc = task_name
    else:
        json_pvc = None

    # Check if we should use shared PVC and skip filer jobs entirely
    shared_pvc_name = os.environ.get('TRANSFER_PVC_NAME')
    
    # Debug logging - inspect JSON structure
    logger.info(f'TASK DEBUG: shared_pvc_name = {shared_pvc_name}')
    logger.info(f'TASK DEBUG: volumes = {data.get("volumes", [])}')
    logger.info(f'TASK DEBUG: inputs = {data.get("inputs", [])}')
    logger.info(f'TASK DEBUG: outputs = {data.get("outputs", [])}')
    
    if (data['volumes'] or data['inputs'] or data['outputs']) and not shared_pvc_name:
        # Original behavior: create filer and run input filer
        logger.info('TASK DEBUG: Using original filer behavior')
        filer = Filer(task_name + '-filer', data, filer_name, filer_version, args.pull_policy_always, json_pvc)

        if os.environ.get('TESK_FTP_USERNAME') is not None:
            filer.set_ftp(
                os.environ['TESK_FTP_USERNAME'],
                os.environ['TESK_FTP_PASSWORD'])

        if os.environ.get('FILER_BACKOFF_LIMIT') is not None:
            filer.set_backoffLimit(int(os.environ['FILER_BACKOFF_LIMIT']))

        pvc = init_pvc(data, filer)
        
    elif shared_pvc_name:
        # Direct PVC mounting: create PVC reference without filer
        logger.info(f'TASK DEBUG: Using shared PVC {shared_pvc_name} - direct path mounting (no file copying)')
        
        #This is just a reference, don't create it in k8s    
        pvc = PVC(shared_pvc_name, 0, args.namespace)

        # Set up basic volume mounts for shared PVC - paths should align naturally
        mounts = generate_mounts(data, pvc)
        pvc.set_volume_mounts(mounts)
        
        # Set created_pvc to None since we're not creating it
        global created_pvc
        created_pvc = None
    else:
        logger.info('TASK DEBUG: No volumes, inputs, or outputs detected - no filer needed')

    logger.info(f'TASK DEBUG: Final PVC name = {pvc.name if pvc else "None"}')

    for executor in data['executors']:
        run_executor(executor, args.namespace, pvc, data)

    # run executors
    logging.debug("Finished running executors")

    # Skip output filer entirely when using shared PVC
    if (data['volumes'] or data['inputs'] or data['outputs']) and not shared_pvc_name:
        # Original behavior: run output filer and delete PVC
        filerjob = Job(
            filer.get_spec('outputs', args.debug),
            task_name + '-outputs-filer',
            args.namespace)

        global created_jobs
        created_jobs.append(filerjob)

        # filerjob.run_to_completion(poll_interval)
        status = filerjob.run_to_completion(poll_interval, check_cancelled, args.pod_timeout)
        if status != 'Complete':
            exit_cancelled('Got status ' + status)
        else:
            pvc.delete()
    elif shared_pvc_name:
        logger.info('Skipping filer jobs - using shared PVC direct mounting (no file copying)')


def newParser():

    parser = argparse.ArgumentParser(description='TaskMaster main module')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        'json',
        help='string containing json TES request, required if -f is not given',
        nargs='?')
    group.add_argument(
        '-f',
        '--file',
        help='TES request as a file or \'-\' for stdin, required if json is not given')

    parser.add_argument(
        '-p',
        '--poll-interval',
        help='Job polling interval',
        default=5)
    parser.add_argument(
        '-pt',
        '--pod-timeout',
        type=int,
        help='Pod creation timeout',
        default=240)
    parser.add_argument(
        '-fn',
        '--filer-name',
        help='Filer image version',
        default='eu.gcr.io/tes-wes/filer')
    parser.add_argument(
        '-fv',
        '--filer-version',
        help='Filer image version',
        default='v0.1.9')
    parser.add_argument(
        '-n',
        '--namespace',
        help='Kubernetes namespace to run in',
        default='default')
    parser.add_argument(
        '-s',
        '--state-file',
        help='State file for state.py script',
        default='/tmp/.teskstate')
    parser.add_argument(
        '-d',
        '--debug',
        help='Set debug mode',
        action='store_true')
    parser.add_argument(
        '--localKubeConfig',
        help='Read k8s configuration from localhost',
        action='store_true')
    parser.add_argument(
        '--pull-policy-always',
        help="set imagePullPolicy = 'Always'",
        action='store_true')


    return parser


def newLogger(loglevel):
    logging.basicConfig(
        format='%(asctime)s %(levelname)s: %(message)s',
        datefmt='%m/%d/%Y %I:%M:%S',
        level=loglevel)
    logging.getLogger('kubernetes.client').setLevel(logging.CRITICAL)
    logger = logging.getLogger(__name__)

    return logger



def main():
    have_json_pvc = False

    parser = newParser()
    global args

    args = parser.parse_args()

    poll_interval = args.poll_interval

    loglevel = logging.INFO  # Changed from ERROR to INFO
    if args.debug:
        loglevel = logging.DEBUG

    global logger
    logger = newLogger(loglevel)
    logger.debug('Starting taskmaster')

    # Get input JSON
    if args.file is None:
        data = json.loads(args.json)
    elif args.file == '-':
        data = json.load(sys.stdin)
    else:
        if args.file.endswith('.gz'):
            with gzip.open(args.file, 'rb') as fh:
                data = json.loads(fh.read())
            have_json_pvc = True
        else:
            with open(args.file) as fh:
                data = json.load(fh)

    # Load kubernetes config file
    if args.localKubeConfig:
        config.load_kube_config()
    else:
        config.load_incluster_config()

    global created_pvc
    created_pvc = None

    # Check if we're cancelled during init
    if check_cancelled():
        exit_cancelled('Cancelled during init')

    run_task(data, args.filer_name, args.filer_version, have_json_pvc)


def clean_on_interrupt():
    logger.debug('Caught interrupt signal, deleting jobs and pvc')

    for job in created_jobs:
        job.delete()



def exit_cancelled(reason='Unknown reason'):
    logger.error('Cancelling taskmaster: ' + reason)
    sys.exit(0)


def check_cancelled():

    labelInfoFile = '/podinfo/labels'

    if not os.path.exists(labelInfoFile):
        return False

    with open(labelInfoFile) as fh:
        for line in fh.readlines():
            name, label = line.split('=')
            logging.debug('Got label: ' + label)
            if label == '"Cancelled"':
                return True

    return False


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clean_on_interrupt()
