#!/usr/bin/env python3
# U280 + 20 usable CFU/HBM windows.
#
# This target exposes 20 usable AXI masters from the expanded CFU directly to
# HBM while keeping the firmware/main_ram pseudo-channel isolated:
#   * main_ram: 64 MB on HBM PC0 at 0x4000_0000 (CPU firmware path).
#   * compute windows: 20 x 64 MB at 0x8000_0000 + p*0x0400_0000.
#     Logical CFU port 0 (cfu_axi)  -> HBM PC8.
#     Logical CFU port 1 (cfu_axi2) -> HBM PC1, ..., port 7 (cfu_axi8) -> HBM PC7.
#     Logical CFU port 8 (cfu_axi9) -> HBM PC9, ..., port 19 (cfu_axi20) -> HBM PC20.
#     Remaining expanded CFU AXI masters are left unconnected.
#   * data staging uses the direct CPU-visible compute windows.
#   * the LiteX CSR bridge is moved to the last 128 KB of the VexRiscv I/O range.
#
# This target requires the 32-port cfu.v (proj/6_10_26/cfu.v) and the
# matching vexriscv/core.py with cfu_axi9..cfu_axi32 interfaces.

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

from litex_boards.targets.xilinx_alveo_u280_cfuaxi_8p_mapped import _CRG


WINDOW_BYTES   = 0x0400_0000   # 64 MB per HBM pseudo-channel
MAIN_RAM_BASE  = 0x4000_0000
SW_ALIAS_BASE  = 0x5000_0000
COMPUTE_BASE   = 0x8000_0000
CPU_SETUP_BASE = 0xD000_0000
CSR_BASE       = 0xfffe_0000
CFU_PORT_COUNT = 20
CFU_DENSE_PORT_COUNT = 8
CFU_SPARSE_PORT_COUNT = 20
COMPUTE0_PHYSICAL_PC = 8


def logical_to_physical_pc(pc):
    if pc == 0:
        return COMPUTE0_PHYSICAL_PC
    if pc < COMPUTE0_PHYSICAL_PC:
        return pc
    return pc + 1


class HBMPortMapControl(Module, AutoCSR):
    """CSR block for the CPU setup window.

    The CFU masters are statically wired one-to-one to physical HBM PCs in this
    target.  The port_map_* CSRs are exposed only to preserve the firmware CSR
    naming used by the mapped targets; cpu_setup_port selects the physical HBM
    PC reached by the CPU staging aperture at 0xD000_0000.
    """
    def __init__(self):
        for port in range(CFU_PORT_COUNT):
            setattr(self, f"port_map_{port}", CSRStorage(
                5,
                reset=logical_to_physical_pc(port),
                name=f"port_map_{port}",
                description=f"Static physical HBM PC for CFU logical port {port}",
            ))
        self.cpu_setup_port = CSRStorage(
            5,
            reset=logical_to_physical_pc(0),
            name="cpu_setup_port",
            description="Physical HBM PC for CPU setup staging window at 0xD0000000",
        )


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
            # HBM AXI clock range starts at 225 MHz; keep the known-good 250 MHz setting.
            sys_clk_freq = int(250e6)
            assert 225e6 <= sys_clk_freq <= 450e6

        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel, with_hbm)

        SoCCore.__init__(self, platform, sys_clk_freq,
            ident="LiteX SoC on U280 + 20-port HBM map",
            **kwargs)
        self.add_constant("CSR_HBM_REDUCED_TRUE_MAP")
        self.add_constant("CFU_PORT_COUNT", CFU_PORT_COUNT)
        self.add_constant("CFU_DENSE_PORT_COUNT", CFU_DENSE_PORT_COUNT)
        self.add_constant("CFU_SPARSE_PORT_COUNT", CFU_SPARSE_PORT_COUNT)
        self.add_constant("CFU_COMPUTE0_PHYSICAL_PC", COMPUTE0_PHYSICAL_PC)
        # proj/6_16_26 firmware sizes sparse and dense HBM work independently.

        # VexRiscv injects the default 0xf0000000 CSR mapping during CPU setup.
        # Override it here before LiteX finalizes and adds the CSR bridge.
        self.mem_map["csr"] = CSR_BASE

        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfuaxi_32p] Added Verilog include path: {incdir}")
        else:
            print("[cfuaxi_32p] WARNING: --cpu-cfu not provided")

        if with_hbm:
            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(
                USPHBM2(platform))

            xci_path = "ip/hbm/hbm_0.xci"
            if not os.path.exists(xci_path):
                os.system("wget https://github.com/litex-hub/litex-boards/files/6893157/hbm_0.xci.txt")
                os.makedirs("ip/hbm", exist_ok=True)
                os.system("mv hbm_0.xci.txt ip/hbm/hbm_0.xci")

            hbm_inputs = [[] for _ in range(32)]

            self.submodules.hbm_port_map = port_map = HBMPortMapControl()
            self.add_csr("hbm_port_map")

            # Firmware/main RAM: direct PC0 path, 64 MB.
            main_lite = AXILiteInterface(data_width=256, address_width=33)
            main_full = AXIInterface(data_width=256, address_width=33, id_width=4)
            main_rebased = AXIInterface(data_width=256, address_width=33, id_width=4)
            self.submodules += AXILite2AXI(main_lite, main_full)
            self.submodules += AXIAddressRebase(
                main_full, main_rebased,
                mask=WINDOW_BYTES - 1,
                offset=0,
            )
            self.bus.add_slave("hbm_main", main_lite,
                SoCRegion(origin=MAIN_RAM_BASE, size=WINDOW_BYTES, cached=True))
            self.bus.add_region("main_ram",
                SoCRegion(origin=MAIN_RAM_BASE, size=WINDOW_BYTES, linker=True))
            hbm_inputs[0].append(main_rebased)

            # CPU-visible compute windows (uncached, IO region).
            def add_cpu_compute_window(pc, name, origin, size, cached=False):
                physical_pc = logical_to_physical_pc(pc)
                lite = AXILiteInterface(data_width=256, address_width=33)
                full = AXIInterface(data_width=256, address_width=33, id_width=4)
                rebased = AXIInterface(data_width=256, address_width=33, id_width=4)
                self.submodules += AXILite2AXI(lite, full)
                self.submodules += AXIAddressRebase(
                    full, rebased,
                    mask=WINDOW_BYTES - 1,
                    offset=0,
                )
                self.bus.add_slave(name, lite,
                    SoCRegion(origin=origin, size=size, cached=cached))
                hbm_inputs[physical_pc].append(rebased)

            # Cached CPU aliases for software-only sparse setup/reference data.
            for pc in range(1, 5):
                add_cpu_compute_window(
                    pc=pc,
                    name=f"hbm_sw_compute{pc}",
                    origin=SW_ALIAS_BASE + WINDOW_BYTES * pc,
                    size=WINDOW_BYTES,
                    cached=True,
                )

            # 20 compute windows at COMPUTE_BASE. Logical port/window 0 keeps
            # the legacy dense-engine mapping to PC8, while PC0 remains
            # dedicated to main_ram.
            for pc in range(CFU_PORT_COUNT):
                add_cpu_compute_window(
                    pc=pc,
                    name=f"hbm_compute{pc}",
                    origin=COMPUTE_BASE + WINDOW_BYTES * pc,
                    size=WINDOW_BYTES,
                )

            # CPU setup staging aperture.  The CFU ports remain statically
            # mapped; this window only gives firmware a reliable way to stage
            # and verify data on arbitrary HBM PCs without depending on the
            # large direct CPU compute decode.
            cpu_setup_lite = AXILiteInterface(data_width=256, address_width=33)
            cpu_setup_full = AXIInterface(data_width=256, address_width=33, id_width=4)
            cpu_setup_rebased = AXIInterface(data_width=256, address_width=33, id_width=4)
            self.submodules += AXILite2AXI(cpu_setup_lite, cpu_setup_full)
            self.submodules += AXIAddressRebase(
                cpu_setup_full, cpu_setup_rebased,
                mask=WINDOW_BYTES - 1,
                offset=0,
            )
            self.bus.add_slave("hbm_setup", cpu_setup_lite,
                SoCRegion(origin=CPU_SETUP_BASE, size=WINDOW_BYTES, cached=False))
            setup_slaves = []
            for pc in range(32):
                bus = AXIInterface(data_width=256, address_width=33, id_width=4)
                hbm_inputs[pc].append(bus)
                setup_slaves.append(
                    (lambda _, p=pc: port_map.cpu_setup_port.storage == p, bus)
                )
            self.submodules += AXIDecoder(cpu_setup_rebased, setup_slaves)

            # 20 CFU AXI masters. Logical port 0 keeps the legacy dense-engine
            # mapping to physical PC8; logical ports 1-7 use PCs 1-7, and
            # logical ports 8-19 use PCs 9-20.
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
            for pc, master in enumerate(cfu_axim):
                physical_pc = logical_to_physical_pc(pc)
                rebased = AXIInterface(
                    data_width=master.data_width,
                    address_width=master.address_width,
                    id_width=master.id_width,
                    clock_domain=master.clock_domain,
                )
                self.submodules += AXIAddressRebase(
                    master, rebased,
                    mask=WINDOW_BYTES - 1,
                    offset=0,
                )
                hbm_inputs[physical_pc].append(rebased)

            for pc, inputs in enumerate(hbm_inputs):
                if inputs:
                    self.submodules += AXIArbiter(inputs, hbm.axi[pc])

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
        description="U280 + 20-port HBM map")
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
