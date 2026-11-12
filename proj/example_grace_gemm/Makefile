#!/bin/env python
# Copyright 2020 Google LLC
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

# This variable lists symbols to define to the C preprocessor
export DEFINES := PLACEHOLDER

# Uncomment this line to use software defined CFU
# DEFINES += CFU_SOFTWARE_DEFINED

# Alveo U280 with HBM: main RAM and the CFU's AXI master both sit on HBM0.
export TARGET ?= xilinx_alveo_u280
export EXTRA_LITEX_ARGS ?= --with-hbm --sys-clk-freq 250e6

# Used only when riscv64-unknown-elf-gcc is not already on PATH.
RISCV_GCC_ROOT ?= $(HOME)/riscv64-unknown-elf-gcc-10.1.0-2020.08.2-x86_64-linux-ubuntu14
ifeq ($(shell command -v riscv64-unknown-elf-gcc 2>/dev/null),)
export PATH := $(RISCV_GCC_ROOT)/bin:$(PATH)
endif

include ../proj.mk
