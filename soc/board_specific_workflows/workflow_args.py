#!/usr/bin/env python3
# Copyright 2021 The CFU-Playground Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
from litex.build.xilinx.vivado import vivado_build_args
from litex.soc.integration.builder import builder_args
from litex.soc.integration.soc_core import soc_core_args
from typing import List


def parse_workflow_args(input: List[str] = None) -> argparse.Namespace:
    """Parses command-line style flags for the workflow.

    All unknown args are discarded to allow multiple parses on args. 

    Args:
        input: An optional list of strings in the style of sys.argv. Will
          default to argparse's interpretation of sys.argv if omitted.
    
    Returns:
        An argparse Namespace with the parsed, known arguments.
    """
    parser = argparse.ArgumentParser(description='LiteX SoC')
    parser.add_argument('--build', action='store_true', help='Build bitstream')
    parser.add_argument('--load', action='store_true', help='Load bitstream')
    parser.add_argument('--toolchain',
                        help=('Specify toolchain for implementing '
                              'gateware (\'vivado\' or \'symbiflow\')'))
    parser.add_argument('--sys-clk-freq', type=float,
                        help='System clock frequency')
    builder_args(parser)
    soc_core_args(parser)
    vivado_build_args(parser)
    parser.add_argument('--with-ethernet',
                        action='store_true',
                        help='Enable Ethernet support')
    parser.add_argument('--with-etherbone',
                        action='store_true',
                        help='Enable Etherbone support')
    parser.add_argument('--with-mapped-flash',
                        action='store_true',
                        help='Add litespi SPI flash')
    parser.add_argument("--with-spi-sdcard",
                        action="store_true",
                        help="Enable SPI-mode SDCard support")
    parser.add_argument("--with-video-framebuffer", 
                        action="store_true", 
                        help="Enable Video Framebuffer (HDMI)")
    parser.add_argument("--with-hbm", 
                        action="store_true",
                        default=False, 
                        help="Enable HBM2 memory (on supported targets)")
    parser.add_argument("--with-analyzer", 
                        action="store_true",
                        default=False, 
                        help="Enable LiteScope analyzer")
    parser.add_argument("--cpu-hbm-ports",
                        default=None,
                        help="Comma-separated HBM port indices exposed on the CPU bus")
    parser.add_argument("--cfu-hbm-port",
                        type=int,
                        default=None,
                        help="HBM port index reserved for the CFU AXI master")
    parser.add_argument("--expose-cfu-port-window",
                        action="store_true",
                        help="Expose the CFU HBM port on the CPU bus via a debug window")
    parser.add_argument("--cfu-port-window-base",
                        default=None,
                        help="Base address for the CFU debug window")
    parser.add_argument("--cfu-port-window-size",
                        default=None,
                        help="Size (bytes) of the CFU debug window")
    parser.add_argument('--target',
                        default='digilent_arty',
                        help='Specify target board')
    parser.set_defaults(csr_csv='csr.csv',
                        uart_name='serial',
                        uart_baudrate=921600,
                        cpu_variant='full+cfu',
                        with_etherbone=False)
    # Return only the known args
    if input:
        args = parser.parse_known_args(input)[0]
    else:
        args = parser.parse_known_args()[0]
        
    if getattr(args, "with_hbm", False):
    
    	if not getattr(args, "sys_clk_freq", None):
    		args.sys_clk_freq = int(250e6)
    		
    	try:
    		args.sys_clk_freq = int(args.sys_clk_freq)
    	except Exception:
    		pass
    
    return args
