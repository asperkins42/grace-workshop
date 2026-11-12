#!/usr/bin/env python3
# U280 + 8 CFU AXI masters to 8 fixed HBM ports (static 1:1 wiring, 64 MiB per port)
#
# Build:
#   make prog TARGET=xilinx_alveo_u280_cfuaxi_8p_64m USE_VIVADO=1 IGNORE_TIMING=1 EXTRA_LITEX_ARGS="--with-hbm"
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


WINDOW_BYTES = 0x0400_0000
MAIN_RAM_BASE = 0x4000_0000
COMPUTE_BASE = 0x8000_0000
CPU_SETUP_BASE = 0xD000_0000
COMPUTE_PCS = (8, 1, 2, 3, 4, 5, 6, 7)


def logical_to_physical_pc(port):
    return 8 if port == 0 else port


def physical_pc_window_base(pc):
    if pc == 0:
        return MAIN_RAM_BASE
    return COMPUTE_BASE + (pc - 1) * WINDOW_BYTES


class HBMPortMapControl(Module, AutoCSR):
    def __init__(self):
        self.port_map_0 = CSRStorage(5, reset=8,
            description="Physical HBM PC for CFU master 0")
        self.port_map_1 = CSRStorage(5, reset=1,
            description="Physical HBM PC for CFU master 1")
        self.port_map_2 = CSRStorage(5, reset=2,
            description="Physical HBM PC for CFU master 2")
        self.port_map_3 = CSRStorage(5, reset=3,
            description="Physical HBM PC for CFU master 3")
        self.port_map_4 = CSRStorage(5, reset=4,
            description="Physical HBM PC for CFU master 4")
        self.port_map_5 = CSRStorage(5, reset=5,
            description="Physical HBM PC for CFU master 5")
        self.port_map_6 = CSRStorage(5, reset=6,
            description="Physical HBM PC for CFU master 6")
        self.port_map_7 = CSRStorage(5, reset=7,
            description="Physical HBM PC for CFU master 7")
        self.cpu_setup_port = CSRStorage(5, reset=8,
            description="Physical HBM PC for CPU setup aperture at 0xd0000000")


class AXIAddressRebase(Module):
    def __init__(self, sink, source, origin=0, offset=0, mask=None):
        self.comb += sink.connect(source, omit={"addr"})
        if mask is None:
            self.comb += [
                source.aw.addr.eq(sink.aw.addr - origin + offset),
                source.ar.addr.eq(sink.ar.addr - origin + offset),
            ]
        else:
            self.comb += [
                source.aw.addr.eq((sink.aw.addr & mask) + offset),
                source.ar.addr.eq((sink.ar.addr & mask) + offset),
            ]


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
            ident="LiteX SoC on U280 + 8-port CFU AXI→HBM (static wiring, 64 MiB/port)", **kwargs)
        self.add_constant("CSR_HBM_8P64M_MAP")
        self.add_constant("CFU_PORT_COUNT", 8)
        self.add_constant("CFU_DENSE_PORT_COUNT", 8)
        self.add_constant("CFU_SPARSE_PORT_COUNT", 8)
        self.add_constant("CFU_COMPUTE0_PHYSICAL_PC", 8)

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

            hbm_inputs = [[] for _ in range(32)]
            self.submodules.hbm_port_map = port_map = HBMPortMapControl()
            self.add_csr("hbm_port_map")

            # Split address map — keeps compute ports in the IO region (non-cached):
            #   hbm0 at 0x4000_0000 (64 MiB, cached)   — firmware / main_ram only
            #   hbm1-7 at 0x8000_0000 + (i-1)*0x0400_0000 (64 MiB each, non-cached)
            #   hbm8 at 0x9C00_0000 (64 MiB, non-cached) — compute port 0
            # Placing compute ports ≥ 0x8000_0000 keeps them in the IO region so the
            # CPU cache is bypassed, matching the original 256 MiB design's behaviour.
            # Compute ports 0-7 map to physical HBM8, HBM1-7 respectively.
            cpu_axim = []
            for i in range(9):
                axi_lite = AXILiteInterface(data_width=256, address_width=33)
                axi_full = AXIInterface(data_width=256, address_width=33, id_width=4)
                self.submodules += AXILite2AXI(axi_lite, axi_full)
                if i == 0:
                    origin = MAIN_RAM_BASE             # firmware window, cached
                    cached = True
                else:
                    origin = COMPUTE_BASE + (i - 1) * WINDOW_BYTES  # compute, non-cached
                    cached = False
                self.bus.add_slave(f"hbm{i}", axi_lite,
                    SoCRegion(origin=origin, size=WINDOW_BYTES, cached=cached))
                cpu_axim.append(axi_full)
                hbm_inputs[i].append(axi_full)

            # main_ram linker region points at hbm0 (firmware/stack only, no matrix data)
            self.bus.add_region("main_ram",
                SoCRegion(origin=MAIN_RAM_BASE, size=WINDOW_BYTES, linker=True))
            self.add_ram("firmware_ram", 0x20000000, 0x8000)

            # CPU setup aperture.  The direct 64 MiB CPU compute windows are kept
            # for inspection, but firmware can use this single aperture plus the
            # cpu_setup_port CSR when the direct LiteX decode aliases windows.
            cpu_setup_lite = AXILiteInterface(data_width=256, address_width=33)
            cpu_setup_full = AXIInterface(data_width=256, address_width=33, id_width=4)
            self.submodules += AXILite2AXI(cpu_setup_lite, cpu_setup_full)
            self.bus.add_slave("hbm_setup", cpu_setup_lite,
                SoCRegion(origin=CPU_SETUP_BASE, size=WINDOW_BYTES, cached=False))
            setup_slaves = []
            for pc in COMPUTE_PCS:
                selected = AXIInterface(data_width=256, address_width=33, id_width=4)
                rebased = AXIInterface(data_width=256, address_width=33, id_width=4)
                self.submodules += AXIAddressRebase(
                    selected, rebased,
                    origin=CPU_SETUP_BASE,
                    offset=physical_pc_window_base(pc),
                )
                hbm_inputs[pc].append(rebased)
                setup_slaves.append(
                    (lambda _, p=pc: port_map.cpu_setup_port.storage == p, selected)
                )
            self.submodules += AXIDecoder(cpu_setup_full, setup_slaves)

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
            for port, master in enumerate(cfu_axim):
                hbm_inputs[logical_to_physical_pc(port)].append(master)

            for pc, inputs in enumerate(hbm_inputs):
                if inputs:
                    self.submodules += AXIArbiter(inputs, hbm.axi[pc])

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
    parser = LiteXSoCArgumentParser(description="U280 + 8-port CFU AXI→HBM (static 1:1 wiring, 64 MiB/port)")
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
