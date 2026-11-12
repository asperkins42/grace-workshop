#!/usr/bin/env python3
# U280 + 8 CFU AXI masters with per-master HBM port-map CSRs (bank-switching).
#
# Each of the 8 CFU masters can be independently redirected to any of the 32 HBM
# pseudo-channels via a 5-bit CSR.  An additional CPU staging window at
# 0xD000_0000 can be pointed at any HBM PC for data-setup DMA.
#
# Default mapping matches the static 8p layout exactly:
#   port_map[0]=8, port_map[1..7]=1..7    (compute ports 0-7)
#   cpu_setup_port=9                       (first otherwise-unused PC)
#
# NOTE: HBM PCs 8-31 require regenerating ip/hbm/hbm_0.xci with all 32 AXI
# ports enabled (~2h Vivado rebuild).  With the default xci (ports 0-7 only)
# the design synthesises and the mapping logic is correct; PCs 8-31 will stall.
# Run at default CSR values first to verify correctness against the 8p bitstream.
#
# Build:
#   make prog TARGET=xilinx_alveo_u280_cfuaxi_8p_mapped USE_VIVADO=1 IGNORE_TIMING=1 \
#             EXTRA_LITEX_ARGS="--with-hbm"
#   make load USE_VIVADO=1 TTY=/dev/ttyUSBx

import os
from migen import *
from litex_boards.platforms import xilinx_alveo_u280

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.builder import *
from litex.soc.interconnect.axi import *
from litex.soc.interconnect.csr import *
from litex.soc.cores.ram.xilinx_usp_hbm2 import USPHBM2

from litedram.modules import MTA18ASF2G72PZ
from litedram.phy import usddrphy
from litedram.common import *


# Port-map CSR module ---------------------------------------------------------

class HBMPortMapControl(Module, AutoCSR):
    """8 independent 5-bit CSRs selecting which of the 32 HBM pseudo-channels
    each CFU AXI master targets, plus one 5-bit CSR for the CPU setup window."""
    def __init__(self):
        self.port_map_0 = CSRStorage(5, reset=8,
            description="Physical HBM PC for CFU master 0 (default 8 = 0xC0000000)")
        self.port_map_1 = CSRStorage(5, reset=1,
            description="Physical HBM PC for CFU master 1 (default 1 = 0x50000000)")
        self.port_map_2 = CSRStorage(5, reset=2,
            description="Physical HBM PC for CFU master 2 (default 2 = 0x60000000)")
        self.port_map_3 = CSRStorage(5, reset=3,
            description="Physical HBM PC for CFU master 3 (default 3 = 0x70000000)")
        self.port_map_4 = CSRStorage(5, reset=4,
            description="Physical HBM PC for CFU master 4 (default 4 = 0x80000000)")
        self.port_map_5 = CSRStorage(5, reset=5,
            description="Physical HBM PC for CFU master 5 (default 5 = 0x90000000)")
        self.port_map_6 = CSRStorage(5, reset=6,
            description="Physical HBM PC for CFU master 6 (default 6 = 0xA0000000)")
        self.port_map_7 = CSRStorage(5, reset=7,
            description="Physical HBM PC for CFU master 7 (default 7 = 0xB0000000)")
        self.cpu_setup_port = CSRStorage(5, reset=9,
            description="Physical HBM PC for CPU setup staging window at 0xD0000000 (default 9)")


# CRG -------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq, ddram_channel, with_hbm):
        if with_hbm:
            self.clock_domains.cd_sys     = ClockDomain()
            self.clock_domains.cd_hbm_ref = ClockDomain()
            self.clock_domains.cd_apb     = ClockDomain()
        else:
            self.rst = Signal()
            self.clock_domains.cd_sys    = ClockDomain()
            self.clock_domains.cd_sys4x  = ClockDomain()
            self.clock_domains.cd_pll4x  = ClockDomain()
            self.clock_domains.cd_idelay = ClockDomain()
        if with_hbm:
            self.submodules.pll = pll = USMMCM(speedgrade=-2)
            pll.register_clkin(platform.request("sysclk", ddram_channel), 100e6)
            pll.create_clkout(self.cd_sys,     sys_clk_freq)
            pll.create_clkout(self.cd_hbm_ref, 100e6)
            pll.create_clkout(self.cd_apb,     100e6)
            platform.add_false_path_constraints(self.cd_sys.clk, self.cd_apb.clk)
        else:
            self.submodules.pll = pll = USMMCM(speedgrade=-2)
            self.comb += pll.reset.eq(self.rst)
            pll.register_clkin(platform.request("sysclk", ddram_channel), 100e6)
            pll.create_clkout(self.cd_pll4x, sys_clk_freq*4, buf=None, with_reset=False)
            pll.create_clkout(self.cd_idelay, 600e6)
            platform.add_false_path_constraints(self.cd_sys.clk, pll.clkin)
            self.specials += [
                Instance("BUFGCE_DIV", p_BUFGCE_DIVIDE=4, i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys.clk),
                Instance("BUFGCE",     i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys4x.clk),
            ]
            self.submodules.idelayctrl = USIDELAYCTRL(cd_ref=self.cd_idelay, cd_sys=self.cd_sys)


# SoC -------------------------------------------------------------------------

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=int(250e6), ddram_channel=0, with_hbm=False, **kwargs):
        platform = xilinx_alveo_u280.Platform()
        if with_hbm:
            assert 225e6 <= sys_clk_freq <= 450e6

        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel, with_hbm)

        SoCCore.__init__(self, platform, sys_clk_freq,
            ident="LiteX SoC on U280 + 8-port CFU AXI→HBM (per-master port-map)", **kwargs)

        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfuaxi_8p_mapped] Added Verilog include path: {incdir}")
        else:
            print("[cfuaxi_8p_mapped] WARNING: --cpu-cfu not provided; cannot add include path")

        if with_hbm:
            # HBM IP (all 32 PCs instantiated; PCs 8-31 require IP regen to activate)
            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(USPHBM2(platform))

            os.system("wget https://github.com/litex-hub/litex-boards/files/6893157/hbm_0.xci.txt")
            os.makedirs("ip/hbm", exist_ok=True)
            os.system("mv hbm_0.xci.txt ip/hbm/hbm_0.xci")

            # Port-map CSR module
            self.submodules.hbm_port_map = port_map = HBMPortMapControl()
            self.add_csr("hbm_port_map")

            # Gather per-master CSR storage signals for decode lambdas
            pm = [
                port_map.port_map_0.storage,
                port_map.port_map_1.storage,
                port_map.port_map_2.storage,
                port_map.port_map_3.storage,
                port_map.port_map_4.storage,
                port_map.port_map_5.storage,
                port_map.port_map_6.storage,
                port_map.port_map_7.storage,
            ]
            cpu_setup_sel = port_map.cpu_setup_port.storage

            # 9 fixed CPU windows: hbm0-3 cached, hbm4-7 non-cached, hbm8 non-cached
            #   hbm0 @ 0x4000_0000 → HBM PC 0  (firmware / main_ram)
            #   hbm1 @ 0x5000_0000 → HBM PC 1
            #   ...
            #   hbm7 @ 0xB000_0000 → HBM PC 7
            #   hbm8 @ 0xC000_0000 → HBM PC 8  (CFU compute port 0 default target)
            cpu_axim = []
            for i in range(9):
                axi_lite = AXILiteInterface(data_width=256, address_width=33)
                axi_full = AXIInterface(data_width=256, address_width=33, id_width=4)
                self.submodules += AXILite2AXI(axi_lite, axi_full)
                cached = (i < 4)
                self.bus.add_slave(f"hbm{i}", axi_lite,
                    SoCRegion(origin=0x4000_0000 + 0x1000_0000*i, size=0x1000_0000, cached=cached))
                cpu_axim.append(axi_full)

            self.bus.add_region("main_ram",
                SoCRegion(origin=0x4000_0000, size=0x1000_0000, linker=True))

            # CPU setup staging window: 0xD000_0000, 256 MB, non-cached.
            # Routes to whichever HBM PC is in cpu_setup_port CSR.
            # Use for data-setup DMA to HBM PCs 9-31 before compute.
            cpu_setup_lite = AXILiteInterface(data_width=256, address_width=33)
            cpu_setup_full = AXIInterface(data_width=256, address_width=33, id_width=4)
            self.submodules += AXILite2AXI(cpu_setup_lite, cpu_setup_full)
            self.bus.add_slave("hbm_setup", cpu_setup_lite,
                SoCRegion(origin=0xD000_0000, size=0x1000_0000, cached=False))

            # 8 CFU AXI masters
            cfu_axim = [
                self.cpu.cfu_axi,
                self.cpu.cfu_axi2,
                self.cpu.cfu_axi3,
                self.cpu.cfu_axi4,
                self.cpu.cfu_axi5,
                self.cpu.cfu_axi6,
                self.cpu.cfu_axi7,
                self.cpu.cfu_axi8,
            ]

            # Per-PC master lists: seed with the 9 static CPU windows
            hbm_inputs = [[] for _ in range(32)]
            static_cpu_pc = [0, 1, 2, 3, 4, 5, 6, 7, 8]
            for cpu_idx, pc in enumerate(static_cpu_pc):
                hbm_inputs[pc].append(cpu_axim[cpu_idx])

            # CPU setup window → AXIDecoder with 32 CSR-driven decode conditions
            setup_slaves = []
            for pc in range(32):
                bus = AXIInterface(data_width=256, address_width=33, id_width=4)
                hbm_inputs[pc].append(bus)
                setup_slaves.append((lambda _, p=pc: cpu_setup_sel == p, bus))
            self.submodules += AXIDecoder(cpu_setup_full, setup_slaves)

            # Each CFU master → AXIDecoder with 32 CSR-driven decode conditions
            for cfu_idx, master in enumerate(cfu_axim):
                pm_sel = pm[cfu_idx]
                slaves = []
                for pc in range(32):
                    bus = AXIInterface(data_width=256, address_width=33, id_width=4)
                    hbm_inputs[pc].append(bus)
                    slaves.append((lambda _, p=pc, s=pm_sel: s == p, bus))
                self.submodules += AXIDecoder(master, slaves)

            # One round-robin arbiter per HBM PC, combining all masters targeting it
            for pc in range(32):
                self.submodules += AXIArbiter(hbm_inputs[pc], hbm.axi[pc])

            # XDC Pblock: keep CFU logic in SLR0 (near HBM on U280)
            platform.add_platform_command(
                "create_pblock pblock_cfu\n"
                "add_cells_to_pblock pblock_cfu [get_cells -hier -filter {{NAME =~ *Cfu*}}]\n"
                "resize_pblock pblock_cfu -add CLOCKREGION_X0Y0:CLOCKREGION_X7Y3"
            )

        else:
            # DDR4 path (unchanged from stock)
            if not self.integrated_main_ram_size:
                self.submodules.ddrphy = usddrphy.USPDDRPHY(
                    platform.request("ddram", ddram_channel),
                    memtype="DDR4", cmd_latency=1, sys_clk_freq=sys_clk_freq,
                    iodelay_clk_freq=600e6, is_rdimm=True)
                self.add_sdram("sdram",
                    phy=self.ddrphy, module=MTA18ASF2G72PZ(sys_clk_freq, "1:4"),
                    size=0x40000000, l2_cache_size=kwargs.get("l2_size", 8192))
            self.add_ram("firmware_ram", 0x20000000, 0x8000)


def main():
    from litex.soc.integration.soc import LiteXSoCArgumentParser
    parser = LiteXSoCArgumentParser(
        description="U280 + 8-port CFU AXI→HBM (per-master port-map bank-switching)")
    tg = parser.add_argument_group(title="Target options")
    tg.add_argument("--build",         action="store_true")
    tg.add_argument("--load",          action="store_true")
    tg.add_argument("--sys-clk-freq",  default=250e6, help="System clock (forced 250 MHz with HBM)")
    tg.add_argument("--ddram-channel", default="0")
    tg.add_argument("--with-hbm",      action="store_true", help="Enable HBM2")
    builder_args(parser); soc_core_args(parser)
    args = parser.parse_args()
    if args.with_hbm:
        args.sys_clk_freq = 250e6

    soc = BaseSoC(
        sys_clk_freq  = int(float(args.sys_clk_freq)),
        ddram_channel = int(args.ddram_channel, 0),
        with_hbm      = args.with_hbm,
        **soc_core_argdict(args)
    )
    bld = Builder(soc, **builder_argdict(args))
    if args.build: bld.build()
    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(bld.get_bitstream_filename(mode="sram"))


if __name__ == "__main__":
    main()
