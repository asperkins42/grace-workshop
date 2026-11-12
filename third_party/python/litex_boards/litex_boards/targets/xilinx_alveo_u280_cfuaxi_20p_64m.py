#!/usr/bin/env python3
# U280 + 20 CFU AXI masters to 20 fixed HBM ports (static wiring, 64 MiB per port)
#
# This is the direct 20-port extension of xilinx_alveo_u280_cfuaxi_8p_64m:
#   * HBM PC0 at 0x4000_0000 is CPU-only firmware/main_ram.
#   * CPU-visible compute windows expose physical HBM PCs 1..20 at
#     0x8000_0000 + (pc - 1) * 64 MiB.
#   * CFU logical port 0 maps to physical HBM PC8.
#   * CFU logical ports 1..7 map to physical HBM PCs 1..7.
#   * CFU logical ports 8..19 map to physical HBM PCs 9..20.

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


WINDOW_BYTES = 0x0400_0000
MAIN_RAM_BASE = 0x4000_0000
COMPUTE_BASE = 0x8000_0000
CFU_PORT_COUNT = 20
CFU_DENSE_PORT_COUNT = 8
CFU_SPARSE_PORT_COUNT = 20
COMPUTE0_PHYSICAL_PC = 8


def logical_to_physical_pc(port):
    if port == 0:
        return COMPUTE0_PHYSICAL_PC
    if port < COMPUTE0_PHYSICAL_PC:
        return port
    return port + 1


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


class AXIAddressMask(Module):
    def __init__(self, sink, source, mask):
        self.comb += sink.connect(source, omit={"addr"})
        self.comb += [
            source.aw.addr.eq(sink.aw.addr & mask),
            source.ar.addr.eq(sink.ar.addr & mask),
        ]


class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=int(150e6), ddram_channel=0, with_hbm=False, **kwargs):
        platform = xilinx_alveo_u280.Platform()
        if with_hbm:
            assert 225e6 <= sys_clk_freq <= 450e6

        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel, with_hbm)

        SoCCore.__init__(self, platform, sys_clk_freq,
            ident="LiteX SoC on U280 + 20-port CFU AXI->HBM (static wiring, 64 MiB/port)", **kwargs)
        self.add_constant("CSR_HBM_8P64M_MAP")
        self.add_constant("CFU_PORT_COUNT", CFU_PORT_COUNT)
        self.add_constant("CFU_DENSE_PORT_COUNT", CFU_DENSE_PORT_COUNT)
        self.add_constant("CFU_SPARSE_PORT_COUNT", CFU_SPARSE_PORT_COUNT)
        self.add_constant("CFU_COMPUTE0_PHYSICAL_PC", COMPUTE0_PHYSICAL_PC)

        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfuaxi_20p_64m] Added Verilog include path: {incdir}")
        else:
            print("[cfuaxi_20p_64m] WARNING: --cpu-cfu not provided; cannot add include path")

        if with_hbm:
            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(USPHBM2(platform))

            if not os.path.exists("ip/hbm/hbm_0.xci"):
                os.system("wget https://github.com/litex-hub/litex-boards/files/6893157/hbm_0.xci.txt")
                os.makedirs("ip/hbm", exist_ok=True)
                os.system("mv hbm_0.xci.txt ip/hbm/hbm_0.xci")

            hbm_inputs = [[] for _ in range(32)]

            main_lite = AXILiteInterface(data_width=256, address_width=33)
            main_full = AXIInterface(data_width=256, address_width=33, id_width=4)
            main_masked = AXIInterface(data_width=256, address_width=33, id_width=4)
            self.submodules += AXILite2AXI(main_lite, main_full)
            self.submodules += AXIAddressMask(main_full, main_masked, WINDOW_BYTES - 1)
            self.bus.add_slave("hbm0", main_lite,
                SoCRegion(origin=MAIN_RAM_BASE, size=WINDOW_BYTES, cached=True))
            self.bus.add_region("main_ram",
                SoCRegion(origin=MAIN_RAM_BASE, size=WINDOW_BYTES, linker=True))
            hbm_inputs[0].append(main_masked)

            # CPU windows are keyed by physical HBM PC so the 20p map is a
            # natural extension of 8p_64m: PC8 stays at 0x9c00_0000.
            cpu_axim = {}
            for pc in range(1, 21):
                axi_lite = AXILiteInterface(data_width=256, address_width=33)
                axi_full = AXIInterface(data_width=256, address_width=33, id_width=4)
                axi_masked = AXIInterface(data_width=256, address_width=33, id_width=4)
                self.submodules += AXILite2AXI(axi_lite, axi_full)
                self.submodules += AXIAddressMask(axi_full, axi_masked, WINDOW_BYTES - 1)
                origin = COMPUTE_BASE + (pc - 1) * WINDOW_BYTES
                self.bus.add_slave(f"hbm{pc}", axi_lite,
                    SoCRegion(origin=origin, size=WINDOW_BYTES, cached=False))
                cpu_axim[pc] = axi_masked
                hbm_inputs[pc].append(axi_masked)

            cfu_axim = [
                self.cpu.cfu_axi,
                self.cpu.cfu_axi2,
                self.cpu.cfu_axi3,
                self.cpu.cfu_axi4,
                self.cpu.cfu_axi5,
                self.cpu.cfu_axi6,
                self.cpu.cfu_axi7,
                self.cpu.cfu_axi8,
                self.cpu.cfu_axi9,
                self.cpu.cfu_axi10,
                self.cpu.cfu_axi11,
                self.cpu.cfu_axi12,
                self.cpu.cfu_axi13,
                self.cpu.cfu_axi14,
                self.cpu.cfu_axi15,
                self.cpu.cfu_axi16,
                self.cpu.cfu_axi17,
                self.cpu.cfu_axi18,
                self.cpu.cfu_axi19,
                self.cpu.cfu_axi20,
            ]
            assert len(cfu_axim) == CFU_PORT_COUNT
            for port, master in enumerate(cfu_axim):
                physical_pc = logical_to_physical_pc(port)
                masked = AXIInterface(
                    data_width=master.data_width,
                    address_width=master.address_width,
                    id_width=master.id_width,
                    clock_domain=master.clock_domain,
                )
                self.submodules += AXIAddressMask(master, masked, WINDOW_BYTES - 1)
                hbm_inputs[physical_pc].append(masked)

            for pc, inputs in enumerate(hbm_inputs):
                if inputs:
                    self.submodules += AXIArbiter(inputs, hbm.axi[pc])

            platform.add_platform_command(
                "create_pblock pblock_cfu\n"
                "add_cells_to_pblock pblock_cfu [get_cells -hier -filter {{NAME =~ *Cfu*}}]\n"
                "resize_pblock pblock_cfu -add CLOCKREGION_X0Y0:CLOCKREGION_X7Y3"
            )

        else:
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
    parser = LiteXSoCArgumentParser(description="U280 + 20-port CFU AXI->HBM (static 1:1 wiring, 64 MiB/port)")
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
