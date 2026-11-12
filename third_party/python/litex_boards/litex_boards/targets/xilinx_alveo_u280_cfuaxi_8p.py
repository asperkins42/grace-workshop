#!/usr/bin/env python3
# U280 + 8 CFU AXI masters to 8 fixed HBM ports (static 1:1 wiring, no sliding window)
#
# Build:
#   make prog TARGET=xilinx_alveo_u280_cfuaxi_8p USE_VIVADO=1 IGNORE_TIMING=1 EXTRA_LITEX_ARGS="--with-hbm"
#   make load USE_VIVADO=1 TTY=/dev/ttyUSBx

import os
from migen import *
from litex_boards.platforms import xilinx_alveo_u280

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.builder import *
from litex.soc.interconnect.axi import *
from litex.soc.cores.ram.xilinx_usp_hbm2 import USPHBM2

from litedram.modules import MTA18ASF2G72PZ
from litedram.phy import usddrphy
from litedram.common import *


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


class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=int(150e6), ddram_channel=0, with_hbm=False, **kwargs):
        platform = xilinx_alveo_u280.Platform()
        if with_hbm:
            assert 225e6 <= sys_clk_freq <= 450e6

        # CRG
        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel, with_hbm)

        # SoC
        SoCCore.__init__(self, platform, sys_clk_freq,
            ident="LiteX SoC on U280 + 8-port CFU AXI→HBM (static wiring)", **kwargs)

        # Add CFU Verilog include path
        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfuaxi_8p] Added Verilog include path: {incdir}")
        else:
            print("[cfuaxi_8p] WARNING: --cpu-cfu not provided; cannot add include path")

        if with_hbm:
            # HBM IP
            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(USPHBM2(platform))

            # Get HBM .xci when it is not already present in the build tree.
            # Local builds normally reuse soc/ip/hbm/hbm_0.xci.
            if not os.path.exists("ip/hbm/hbm_0.xci"):
                os.system("wget https://github.com/litex-hub/litex-boards/files/6893157/hbm_0.xci.txt")
                os.makedirs("ip/hbm", exist_ok=True)
                os.system("mv hbm_0.xci.txt ip/hbm/hbm_0.xci")

            # 9 CPU windows:
            #   hbm0-hbm7 at 0x4000_0000-0xBFFF_FFFF  (256 MiB each)
            #   hbm8       at 0xC000_0000               (256 MiB, compute port 0)
            # hbm0 is firmware/main_ram only (no CFU master).
            # Compute ports 0-7 map to physical HBM8, HBM1-7 respectively.
            cpu_axim = []
            for i in range(9):
                axi_lite = AXILiteInterface(data_width=256, address_width=33)
                axi_full = AXIInterface(data_width=256, address_width=33, id_width=4)
                self.submodules += AXILite2AXI(axi_lite, axi_full)
                # Ports 4-7 (0x80000000+) and port 8 (0xC0000000) are non-cached IO region.
                cached = (i < 4)
                self.bus.add_slave(f"hbm{i}", axi_lite,
                    SoCRegion(origin=0x4000_0000 + 0x1000_0000*i, size=0x1000_0000, cached=cached))
                cpu_axim.append(axi_full)

            # main_ram linker region points at hbm0 (firmware/stack only, no matrix data)
            self.bus.add_region("main_ram",
                SoCRegion(origin=0x4000_0000, size=0x1000_0000, linker=True))

            # 8 CFU AXI masters (ports 0-3 via cfu_axi..cfu_axi4, ports 4-7 via cfu_axi5..cfu_axi8)
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

            # HBM0: CPU-only (firmware/main_ram). No CFU master — single-master arbiter.
            self.submodules += AXIArbiter([cpu_axim[0]], hbm.axi[0])
            # HBM1-7: CPU + CFU compute ports 1-7 (unchanged 1:1 mapping).
            for i in range(1, 8):
                self.submodules += AXIArbiter([cpu_axim[i], cfu_axim[i]], hbm.axi[i])
            # HBM8: CPU + CFU compute port 0 (m_axi). Clean 256 MiB, no firmware offset needed.
            self.submodules += AXIArbiter([cpu_axim[8], cfu_axim[0]], hbm.axi[8])

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
    parser = LiteXSoCArgumentParser(description="U280 + 8-port CFU AXI→HBM (static 1:1 wiring)")
    tg = parser.add_argument_group(title="Target options")
    tg.add_argument("--build",           action="store_true")
    tg.add_argument("--load",            action="store_true")
    tg.add_argument("--sys-clk-freq",    default=150e6, help="Use 150 MHz with DDR4, forced to 250 MHz with HBM")
    tg.add_argument("--ddram-channel",   default="0")
    tg.add_argument("--with-hbm",        action="store_true", help="Enable HBM2")
    builder_args(parser); soc_core_args(parser)
    args = parser.parse_args()
    if args.with_hbm:
        args.sys_clk_freq = 250e6

    soc = BaseSoC(
        sys_clk_freq = int(float(args.sys_clk_freq)),
        ddram_channel= int(args.ddram_channel, 0),
        with_hbm     = args.with_hbm,
        **soc_core_argdict(args)
    )
    bld = Builder(soc, **builder_argdict(args))
    if args.build: bld.build()
    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(bld.get_bitstream_filename(mode="sram"))


if __name__ == "__main__":
    main()
