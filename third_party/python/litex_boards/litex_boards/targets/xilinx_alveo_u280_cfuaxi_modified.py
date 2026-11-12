#!/usr/bin/env python3
# U280 + CFU AXI master to HBM port-4
#
# Build:
#   make prog TARGET=xilinx_alveo_u280_cfuaxi USE_VIVADO=1 IGNORE_TIMING=1 EXTRA_LITEX_ARGS="--with-hbm"
#   make load USE_VIVADO=1 TTY=/dev/ttyUSBx

import os
from migen import *
from litex_boards.platforms import xilinx_alveo_u280  # platform/pins  (stock)  :contentReference[oaicite:2]{index=2}

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

# CRG (same pattern as stock)
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
        self.submodules.pll = pll = USMMCM(speedgrade=-2)
        pll.register_clkin(platform.request("sysclk", ddram_channel), 100e6)
        if with_hbm:
            pll.create_clkout(self.cd_sys,     sys_clk_freq)
            pll.create_clkout(self.cd_hbm_ref, 100e6)
            pll.create_clkout(self.cd_apb,     100e6)
            platform.add_false_path_constraints(self.cd_sys.clk, self.cd_apb.clk)
        else:
            self.comb += pll.reset.eq(self.rst)
            pll.create_clkout(self.cd_pll4x, sys_clk_freq*4, buf=None, with_reset=False)
            pll.create_clkout(self.cd_idelay, 600e6)
            self.specials += [
                Instance("BUFGCE_DIV", p_BUFGCE_DIVIDE=4, i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys.clk),
                Instance("BUFGCE",     i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys4x.clk),
            ]
            self.submodules.idelayctrl = USIDELAYCTRL(cd_ref=self.cd_idelay, cd_sys=self.cd_sys)

class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=int(250e6), ddram_channel=0, with_hbm=False, **kwargs):
        platform = xilinx_alveo_u280.Platform()                                      # :contentReference[oaicite:3]{index=3}
        if with_hbm:
            assert 225e6 <= sys_clk_freq <= 450e6

        # CRG
        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel, with_hbm)

        # SoC
        SoCCore.__init__(self, platform, sys_clk_freq, ident="LiteX SoC on U280 + CFU AXI→HBM", **kwargs)

        # tell BIOS to load to 0x50000000 instead.
        # SoCCore.mem_map["main_ram"] = 0x5000_0000
        self.mem_map["main_ram"] = 0x5000_0000
        # self.bus.add_region("main_ram", SoCRegion(origin=0x5000_0000, size=0x1000_0000, linker=True))
        self.add_config("MAIN_RAM_BASE", 0x50000000)

        # Make sure Vivado can resolve `include "cfu_axi_master.v"` relative to your cfu.v
        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            # Allow Verilog `include` to find files in the project directory
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfuaxi] Added Verilog include path: {incdir}")
        else:
            print("[cfuaxi] WARNING: --cpu-cfu not provided; cannot add include path")

        if with_hbm:
            # HBM IP (AXI domain renamed to sys)
            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(USPHBM2(platform))
        
            # Ensure the XCI is present (same behavior as before)
            os.system("wget https://github.com/litex-hub/litex-boards/files/6893157/hbm_0.xci.txt")
            os.makedirs("ip/hbm", exist_ok=True)
            os.system("mv hbm_0.xci.txt ip/hbm/hbm_0.xci")
        
            
            # Connect four of the HBM's AXI interfaces to the main bus of the SoC.
            for i in range(1, 4):
                axi_hbm      = hbm.axi[i]
                axi_lite_hbm = AXILiteInterface(data_width=256, address_width=33)
                self.submodules += AXILite2AXI(axi_lite_hbm, axi_hbm)
                self.bus.add_slave(f"hbm{i}", axi_lite_hbm, SoCRegion(origin=0x4000_0000 + 0x1000_0000*i, size=0x1000_0000)) # 256MB.
            
            # # Link HBM2 channel 0 as main RAM
            # self.bus.add_region("main_ram", SoCRegion(origin=0x5000_0000, size=0x1000_0000, linker=True)) # 256MB.
# 
            # # ---------- CFU AXI master: route EXCLUSIVELY to HBM port-0 ----------
            # # No CPU window for port-0 (so it isn't doubly mapped).
            # cfu_axi = self.cpu.cfu_axi  # provided by VexRiscv.add_cfu()
            # def _all_addrs(_): return True
            # # axi_hbm      = hbm.axi[0]
            # self.submodules.cfu_hbm_xbar = AXICrossbar(
            #     masters=[cfu_axi],
            #     slaves=[(_all_addrs, hbm.axi[0])]
            # )

            # ---------- CFU AXI master: 32-bit -> upconvert -> 256-bit -> HBM port-0 ----------
            cfu_axi = self.cpu.cfu_axi  # CFU AXI-Full master (likely 32-bit data)

            # Upsize CFU AXI to match HBM user port width (256-bit)
            cfu_axi_wide = AXIInterface(
                address_width = cfu_axi.address_width,
                data_width    = 256,
                id_width      = getattr(cfu_axi, "id_width", 1)
            )
            self.submodules.cfu_axi_up = AXIUpConverter(cfu_axi, cfu_axi_wide)

            # Route the wide CFU bus to HBM port-0
            def _all_addrs(_): return True
            self.submodules.cfu_hbm_xbar = AXICrossbar(
                masters=[cfu_axi_wide],
                slaves =[(_all_addrs, hbm.axi[0])]
            )

            # (Optional) build-time sanity print
            print(f"[CFU AXI] {cfu_axi.data_width}-bit -> upconvert -> 256-bit; HBM0 is {hbm.axi[0].data_width}-bit")



        else:
            # DDR4 path (unchanged)
            if not self.integrated_main_ram_size:
                self.submodules.ddrphy = usddrphy.USPDDRPHY(platform.request("ddram", ddram_channel),
                    memtype="DDR4", cmd_latency=1, sys_clk_freq=sys_clk_freq,
                    iodelay_clk_freq=600e6, is_rdimm=True)
                self.add_sdram("sdram",
                    phy=self.ddrphy, module=MTA18ASF2G72PZ(sys_clk_freq, "1:4"),
                    size=0x40000000, l2_cache_size=kwargs.get("l2_size", 8192))
            self.add_ram("firmware_ram", 0x20000000, 0x8000)

def main():
    from litex.soc.integration.soc import LiteXSoCArgumentParser
    parser = LiteXSoCArgumentParser(description="U280 + CFU AXI→HBM")
    tg = parser.add_argument_group(title="Target options")
    tg.add_argument("--build",           action="store_true")
    tg.add_argument("--load",            action="store_true")
    tg.add_argument("--sys-clk-freq",    default=250e6, help="Use 250 MHz with HBM")
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
