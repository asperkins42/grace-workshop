#!/usr/bin/env python3
# U280 + staged sparse 32-port reduced HBM map.
#
# This target is the staging point between the current 8-master CFU design and a
# true 32-concurrent-port sparse design:
#   * main_ram: 64 MB on HBM PC0 at 0x4000_0000.
#   * default compute windows: 9 x 64 MB at 0x8000_0000 + p*0x0400_0000.
#     Set EXPOSE_ALL_REDUCED_CPU_WINDOWS for standalone 32-window map checks.
#   * compute window 0 is rebased to an unused HBM PC offset 0x0400_0000,
#     keeping PC0 dedicated to firmware/main_ram.
#   * compute windows 1..8 are rebased to HBM PCs 1..8 offset 0 by default;
#     the standalone map-check flag extends this through PC31.
#   * data staging uses the direct CPU-visible compute windows.
#   * the LiteX CSR bridge is moved to the last 128 KB of the VexRiscv I/O
#     range; in full map-check mode, PC31 is split below it.
#
# The CFU path still exposes the current eight AXI masters. They are wired
# directly to reduced compute PCs, with logical port 0 moved off PC0 to keep
# the firmware/main_ram pseudo-channel isolated. Full
# 32-way concurrency still requires the VexRiscv CFU AXI exports, cfu.v
# top-level ports, sparse engines, and HBM IP to be expanded.

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

from litex_boards.targets.xilinx_alveo_u280_cfuaxi_8p_mapped import _CRG


REDUCED_WINDOW_BYTES = 0x0400_0000
MAIN_RAM_BASE        = 0x4000_0000
SW_ALIAS_BASE        = 0x5000_0000
COMPUTE_BASE         = 0x8000_0000
CSR_BASE             = 0xfffe_0000

# Keep firmware/main_ram alone on HBM PC0. Logical compute window/CFU port 0
# still uses CPU address 0x8000_0000, but physically targets this pseudo-channel
# at offset 64 MB. PC9 is unused in the default reduced map.
COMPUTE0_PHYSICAL_PC = 9

# PC31 reaches the top of the 32-bit VexRiscv I/O region. LiteX rejects regions
# ending exactly at 4 GiB and also needs space for the CSR bridge, so expose PC31
# to the CPU as power-of-two sub-windows below CSR_BASE.
PC31_CPU_SEGMENT_SIZES = [
    0x0200_0000,
    0x0100_0000,
    0x0080_0000,
    0x0040_0000,
    0x0020_0000,
    0x0010_0000,
    0x0008_0000,
    0x0004_0000,
    0x0002_0000,
]

# Keep the default image focused on the currently passing 8-engine sparse
# regression path. Exposing all 32 reduced CPU windows creates a very large
# Wishbone decode/interconnect and has shown HBM memtest corruption when timing
# is ignored. Set this True only for standalone address-map validation.
EXPOSE_ALL_REDUCED_CPU_WINDOWS = False


class AXIAddressRebase(Module):
    """Forward an AXI bus while rebasing AW/AR addresses.

    If mask is supplied, the outgoing address is `(incoming & mask) + offset`.
    Otherwise the outgoing address is `incoming - origin + offset`.
    """
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


class BaseSoC(SoCCore):
    def __init__(self, sys_clk_freq=int(250e6), ddram_channel=0,
                 with_hbm=False, **kwargs):
        platform = xilinx_alveo_u280.Platform()
        if with_hbm:
            assert 225e6 <= sys_clk_freq <= 450e6

        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel, with_hbm)

        SoCCore.__init__(self, platform, sys_clk_freq,
            ident="LiteX SoC on U280 + staged 32-port reduced sparse HBM map",
            **kwargs)
        self.add_constant("CSR_HBM_REDUCED_TRUE_MAP")

        # VexRiscv injects the default 0xf0000000 CSR mapping during CPU setup.
        # Override it here before LiteX finalizes and adds the CSR bridge.
        self.mem_map["csr"] = CSR_BASE

        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfuaxi_32p_reduced] Added Verilog include path: {incdir}")
        else:
            print("[cfuaxi_32p_reduced] WARNING: --cpu-cfu not provided")

        if with_hbm:
            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(
                USPHBM2(platform))

            xci_path = "ip/hbm/hbm_0.xci"
            if not os.path.exists(xci_path):
                os.system("wget https://github.com/litex-hub/litex-boards/files/6893157/hbm_0.xci.txt")
                os.makedirs("ip/hbm", exist_ok=True)
                os.system("mv hbm_0.xci.txt ip/hbm/hbm_0.xci")

            hbm_inputs = [[] for _ in range(32)]

            # Firmware/main RAM: direct PC0 path, 64 MB.
            main_lite = AXILiteInterface(data_width=256, address_width=33)
            main_full = AXIInterface(data_width=256, address_width=33, id_width=4)
            self.submodules += AXILite2AXI(main_lite, main_full)
            self.bus.add_slave("hbm_main", main_lite,
                SoCRegion(origin=MAIN_RAM_BASE, size=REDUCED_WINDOW_BYTES,
                          cached=True))
            self.bus.add_region("main_ram",
                SoCRegion(origin=MAIN_RAM_BASE, size=REDUCED_WINDOW_BYTES,
                          linker=True))
            hbm_inputs[0].append(main_full)

            # CPU-visible reduced compute windows. These replace the old 256 MB
            # hbm0..hbm8 compute windows in this target.
            def add_cpu_compute_segment(pc, name, origin, size, cached=False):
                physical_pc = COMPUTE0_PHYSICAL_PC if pc == 0 else pc
                lite = AXILiteInterface(data_width=256, address_width=33)
                full = AXIInterface(data_width=256, address_width=33, id_width=4)
                rebased = AXIInterface(data_width=256, address_width=33, id_width=4)
                pc_offset = REDUCED_WINDOW_BYTES if pc == 0 else 0
                self.submodules += AXILite2AXI(lite, full)
                self.submodules += AXIAddressRebase(
                    full, rebased,
                    mask=REDUCED_WINDOW_BYTES - 1,
                    offset=pc_offset,
                )
                self.bus.add_slave(name, lite,
                    SoCRegion(origin=origin, size=size, cached=cached))
                hbm_inputs[physical_pc].append(rebased)

            # Cached CPU aliases for software-only sparse setup/reference data.
            # The VexRiscv IO region makes 0x8000_0000+ uncached, which is too
            # slow for the large CSR software baselines. Hardware still uses the
            # reduced compute windows above.
            for pc in range(1, 5):
                add_cpu_compute_segment(
                    pc=pc,
                    name=f"hbm_sw_compute{pc}",
                    origin=SW_ALIAS_BASE + REDUCED_WINDOW_BYTES*pc,
                    size=REDUCED_WINDOW_BYTES,
                    cached=True,
                )

            compute_window_count = 31 if EXPOSE_ALL_REDUCED_CPU_WINDOWS else 9
            for pc in range(compute_window_count):
                add_cpu_compute_segment(
                    pc=pc,
                    name=f"hbm_compute{pc}",
                    origin=COMPUTE_BASE + REDUCED_WINDOW_BYTES*pc,
                    size=REDUCED_WINDOW_BYTES,
                )

            if EXPOSE_ALL_REDUCED_CPU_WINDOWS:
                pc31_origin = COMPUTE_BASE + REDUCED_WINDOW_BYTES*31
                for segment, size in enumerate(PC31_CPU_SEGMENT_SIZES):
                    add_cpu_compute_segment(
                        pc=31,
                        name="hbm_compute31" if segment == 0 else f"hbm_compute31_{segment}",
                        origin=pc31_origin,
                        size=size,
                    )
                    pc31_origin += size

            # Current CFU has eight AXI masters. Wire them directly to reduced
            # compute PCs; logical port 0 skips PC0 so firmware/main_ram does
            # not share the same HBM arbiter with CSR_GATHER x-cache traffic.
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
            for cfu_idx, master in enumerate(cfu_axim):
                physical_pc = COMPUTE0_PHYSICAL_PC if cfu_idx == 0 else cfu_idx
                pc_offset = REDUCED_WINDOW_BYTES if cfu_idx == 0 else 0
                rebased = AXIInterface(
                    data_width=master.data_width,
                    address_width=master.address_width,
                    id_width=master.id_width,
                    clock_domain=master.clock_domain,
                )
                self.submodules += AXIAddressRebase(
                    master, rebased,
                    mask=REDUCED_WINDOW_BYTES - 1,
                    offset=pc_offset,
                )
                hbm_inputs[physical_pc].append(rebased)

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
    parser = LiteXSoCArgumentParser(
        description="U280 + staged 32-port reduced sparse HBM map")
    tg = parser.add_argument_group(title="Target options")
    tg.add_argument("--build",         action="store_true")
    tg.add_argument("--load",          action="store_true")
    tg.add_argument("--sys-clk-freq",  default=250e6)
    tg.add_argument("--ddram-channel", default="0")
    tg.add_argument("--with-hbm",      action="store_true")
    builder_args(parser)
    soc_core_args(parser)
    args = parser.parse_args()
    if args.with_hbm:
        args.sys_clk_freq = 250e6

    soc = BaseSoC(
        sys_clk_freq=int(float(args.sys_clk_freq)),
        ddram_channel=int(args.ddram_channel, 0),
        with_hbm=args.with_hbm,
        **soc_core_argdict(args)
    )
    builder = Builder(soc, **builder_argdict(args))
    if args.build:
        builder.build()
    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"))


if __name__ == "__main__":
    main()
