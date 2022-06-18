# coding: utf-8
"""
idawasm - An IDA Pro plugin that implements the loader and processor to disassembly the WebAssembly Binary (.wasm) files.
"""

# Author details
__author__ = "Willi Ballenthin, Takumi Akiyama, Huang Xiang-Yao"
__author_email__ = "william.ballenthin@fireeye.com, t.akiym@gmail.com, 4848285@qq.com"

# The project's main homepage.
__url__ = "https://github.com/huangxiangyao/idawasm"

# Versions should comply with PEP440.  For a discussion on single-sourcing
# the version across setup.py and the project code, see
# https://packaging.python.org/en/latest/single_source_version.html
__version__ = "0.2.6"

__license__ = "Apache License 2.0"

# What does your project relate to?
__keywords__ = "ida, loaders,  wasm"

import os
import platform
import shutil

# Third party Module, should be installed by prerequisites
import requests
import logging

logger = logging.getLogger(__name__)

class ExitException(Exception):
    pass

def main():
    usage = 'Description: An IDA Pro plugin that implements the loader and processor to disassembly the WebAssembly Binary (.wasm) files.\n'\
        'Version: {}\n'\
        'Requirements: \n'\
        '- IDA Pro 7.4+ and Python 3.8+\n'\
        '- Admin Privileges\n'\
        '  (usually needed to copy plugin into IDA directory)\n\n'
    print(usage.format(__version__))

    try:
        working_dir = os.path.dirname(__file__)
        #print("working_dir", working_dir)

        ida_root_path = os.getcwd()
        try:
            ida_root_path_t = input('Enter full path to IDA\'s root folder [{}]: '.format(ida_root_path))
            if not (ida_root_path_t == ''):
                ida_root_path = ida_root_path_t
        except KeyboardInterrupt:
            return
        if not os.path.exists(ida_root_path):
            print('[Error] Path provided does not exist: {}'.format(ida_root_path))
            raise ExitException()
        if not os.path.exists(os.path.join(ida_root_path, 'loaders')):
            print('[Error] No IDA in folder: {}'.format(ida_root_path))
            raise ExitException()
        #print("ida_root_path", ida_root_path)

        if not os.path.isdir(ida_root_path):
            print('[Error] Path provided is not a directory.')
            raise ExitException()

        if (os.name == 'posix') and (platform.system() == 'Darwin'):
            #   Install for Mac
            requests_path = os.path.dirname(requests.__file__)
            #ida_path = os.path.dirname(ida_root_path)
            ida_path = ida_root_path

            #   Copy requests to IDA's python folder
            print('- Copying FIRST dependencies to IDA\'s python folder...')
            cmd = 'cp -r {} {}/python/requests/'.format(requests_path, ida_path)
            os.system(cmd)
        elif (os.name == 'nt') and (platform.system() == 'Windows'):
            #   Install for Windows - no additional steps required
            pass
        elif (os.name == 'posix') and (platform.system() == 'Linux'):
            #   Currently not supported due to having no ida to bring in the dependencies for Linux
            print('- Unfortunately the current OS is not supported.')
            raise ExitException()
        else:
            #   Doesn't support other systems
            print('- Unfortunately the current OS is not supported.')
            raise ExitException()
        
        # src files
        loader_path = os.path.join(os.path.dirname(working_dir), 'loaders', 'wasm_loader.py')
        #print("loader_path", loader_path)
        
        proc_path = os.path.join(os.path.dirname(working_dir), 'procs', 'wasm_proc.py')
        #print("proc_path", proc_path)

        #   Copy plugin to IDA's loaders directory
        ida_loaders_path = os.path.join(ida_root_path, 'loaders')
        #print("ida_loaders_path", ida_loaders_path)
        ida_procs_path = os.path.join(ida_root_path, 'procs')
        #print("ida_procs_path", ida_procs_path)
        msg = '\nCopy {} to IDA...\n'\
              '- from: {}\n'\
              '- to: {}\n'        
        print(msg.format('wasm_loader.py', os.path.dirname(loader_path), ida_loaders_path))
        shutil.copy(loader_path, ida_loaders_path)
        msg = ( '* An IDA loader has been installed:  {}\n')
        print(msg.format('wasm_loader.py'))
                
        msg = '\nCopy {} to IDA...\n'\
              '- from: {}\n'\
              '- to: {}\n'        
        print(msg.format('wasm_proc.py', os.path.dirname(proc_path), ida_procs_path))
        shutil.copy(proc_path, ida_procs_path)
        msg = ( '* An IDA processor has been installed:  {}\n')
        print(msg.format('wasm_proc.py'))

    except ExitException:
        pass

    finally:
        print('\n...exiting...')
