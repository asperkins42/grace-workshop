#!/usr/bin/env python3
# U280 + 24 CFU AXI masters, PC-local HBM map + four-context GRACE-KEM.
#
# W5 additions over ..._pcmap.py:
#  - pblock_cpu: pins the VexRiscv (whose Wishbone<->AXI converter logic
#    dominated the -1.6 ns worst paths, 77% route delay from placement
#    spread) into the clock regions directly above the HBM interface.
#  - Vivado directives: place ExtraTimingOpt, post-place phys_opt
#    AggressiveExplore, route AggressiveExplore (VecMADS-paper-style).
#  - (cfu.v side, proj/7_15_26: registered R-channel stage per port.)
#
# Derived from xilinx_alveo_u280_cfuaxi_20p_64m_w5.py. It retains the local-PC
# AXIAddressRebase and exposes four additional CFU masters: the existing DMA
# on PC21 plus ML-KEM contexts on PC22--PC24.
#
# Why: the HBM IP is built with the AXI switch ENABLED (hbm_0.xci:
# USER_SWITCH_ENABLE=TRUE), so the serving PC is selected by ADDRESS bits
# [32:28], not by the AXI port. The old map placed the logical windows in
# 0x8000_0000-0xCFFF_FFFF = only FIVE PCs (PC8-12, 4 windows each), pinning
# aggregate delivered bandwidth at ~28 GB/s regardless of fetcher behavior
# (measured identically across W2/W2.1/W2.2). With this remap every AXI port
# compute port talks to its LOCAL PC (DMA retains cross-PC routing); per-port
# ceiling returns to the 250 MHz AXI limit (8 GB/s per active port).
#
# Existing CPU-visible and CFU-visible addresses are unchanged. PC22--PC24
# use their normal logical window addresses in the CFU and the selectable
# 0xD0000000 CPU setup aperture for firmware staging.
#
# Build:
#   make prog TARGET=xilinx_alveo_u280_cfuaxi_24p_64m_w5_kem4 USE_VIVADO=1 EXTRA_LITEX_ARGS="--with-hbm"
#   make load USE_VIVADO=1 TTY=/dev/ttyUSBx

import os
from migen import *
from litex_boards.platforms import xilinx_alveo_u280

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.builder import *
from litex.soc.interconnect.axi import *
from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import *
from litex.soc.cores.ram.xilinx_usp_hbm2 import USPHBM2

from litedram.modules import MTA18ASF2G72PZ
from litedram.phy import usddrphy
from litedram.common import *


WINDOW_BYTES = 0x0400_0000
MAIN_RAM_BASE = 0x4000_0000
COMPUTE_BASE = 0x8000_0000
CPU_SETUP_BASE = 0xD000_0000
GRACE_DMA_PC = 21
CFU_PORT_COUNT = 24
CFU_DENSE_PORT_COUNT = 8
CFU_SPARSE_PORT_COUNT = 20
COMPUTE0_PHYSICAL_PC = 8
MLKEM_SETUP_PCS = (22, 23, 24)
COMPUTE_PCS = tuple(
    COMPUTE0_PHYSICAL_PC if port == 0 else port if port < COMPUTE0_PHYSICAL_PC else port + 1
    for port in range(CFU_PORT_COUNT)
)


def logical_to_physical_pc(port):
    if port == 0:
        return COMPUTE0_PHYSICAL_PC
    if port < COMPUTE0_PHYSICAL_PC:
        return port
    return port + 1


class HBMPortMapControl(Module, AutoCSR):
    def __init__(self):
        for port, pc in enumerate(COMPUTE_PCS):
            setattr(self, f"port_map_{port}", CSRStorage(5, reset=pc,
                name=f"port_map_{port}",
                description=f"Physical HBM PC for CFU master {port}"))
        self.cpu_setup_port = CSRStorage(5, reset=COMPUTE0_PHYSICAL_PC,
            name="cpu_setup_port",
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
            # All mapped windows are 64 MiB aligned and mask selects only
            # address bits below that boundary.  OR expresses the mapping as
            # wiring/constants and prevents Vivado from inferring a carry
            # chain for two disjoint bit fields.
            assert (offset & mask) == 0
            self.comb += [
                source.aw.addr.eq((sink.aw.addr & mask) | offset),
                source.ar.addr.eq((sink.ar.addr & mask) | offset),
            ]


class AXIHBMPortRebase(AXIAddressRebase):
    """Translate the addresses presented to a physical HBM AXI port."""
    def __init__(self, sink, source, pc):
        if pc == GRACE_DMA_PC:
            # Firmware encodes target physical addresses as
            # (target + 0xd0000000 - 21*0x10000000) modulo 2**33.
            # Preserve the PC bits: DMA uses the HBM switch to copy between
            # channels. Masking to 64 MiB silently redirects both ends to PC21.
            super().__init__(sink, source, origin=CPU_SETUP_BASE,
                             offset=pc * 0x1000_0000)
        else:
            super().__init__(sink, source, offset=pc * 0x1000_0000,
                             mask=WINDOW_BYTES - 1)


class AXILiteAtomicUpConverter(Module):
    """Single-outstanding 32-to-256-bit AXI-Lite converter.

    The read half is deliberately transaction-aware: it accepts one AR, saves
    its word lane, and captures the selected 32-bit word and response together
    before acknowledging another request.  This both cuts the HBM-to-CPU read
    timing path and prevents either the lane tag or response payload from
    advancing independently through generic stream pipeline stages.
    """
    def __init__(self, master, slave):
        assert isinstance(master, AXILiteInterface)
        assert isinstance(slave, AXILiteInterface)
        dw_from = len(master.r.data)
        dw_to = len(slave.r.data)
        assert dw_to > dw_from and (dw_to % dw_from) == 0
        ratio = dw_to // dw_from
        master_align = log2_int(master.data_width // 8)
        slave_align = log2_int(slave.data_width // 8)

        wr_word = Signal(log2_int(ratio))
        wr_word_r = Signal(log2_int(ratio))
        rd_word_r = Signal(log2_int(ratio))
        rd_busy_r = Signal(reset=0)
        rd_valid_r = Signal(reset=0)
        rd_data_r = Signal(dw_from)
        rd_resp_r = Signal(len(master.r.resp))
        rd_first_r = Signal(reset=0)
        rd_last_r = Signal(reset=0)
        selected_rdata = Signal(dw_from)

        # Write channels retain LiteX's normal narrow-to-wide behavior.  The
        # write response is pipelined later, at the full-width AXI boundary.
        self.comb += [
            master.aw.connect(slave.aw, omit={"addr"}),
            master.w.connect(slave.w, omit={"strb", "data"}),
            slave.b.connect(master.b),
            slave.aw.addr[slave_align:].eq(master.aw.addr[slave_align:]),
        ]

        # The write address and data are issued together by Wishbone2AXILite;
        # retain LiteX's current-address behavior for that channel.
        self.sync += If(master.aw.valid, wr_word_r.eq(wr_word))
        self.comb += If(master.aw.valid,
            wr_word.eq(master.aw.addr[master_align:slave_align])
        ).Else(
            wr_word.eq(wr_word_r)
        )

        wr_cases = {}
        rd_cases = {}
        for i in range(ratio):
            strb_from = i * dw_from // 8
            strb_to = (i + 1) * dw_from // 8
            data_from = i * dw_from
            data_to = (i + 1) * dw_from
            wr_cases[i] = [
                slave.w.strb[strb_from:strb_to].eq(master.w.strb),
                slave.w.data[data_from:data_to].eq(master.w.data),
            ]
            rd_cases[i] = selected_rdata.eq(slave.r.data[data_from:data_to])

        self.comb += Case(wr_word, wr_cases)
        self.comb += Case(rd_word_r, rd_cases)

        # Read-address acceptance is blocked until the preceding response has
        # been consumed.  This conservative contract matches the CPU bridge
        # and makes the saved lane unambiguously belong to the captured R.
        self.comb += [
            slave.ar.valid.eq(master.ar.valid & ~rd_busy_r),
            master.ar.ready.eq(slave.ar.ready & ~rd_busy_r),
            slave.ar.addr[slave_align:].eq(master.ar.addr[slave_align:]),
            slave.ar.first.eq(master.ar.first),
            slave.ar.last.eq(master.ar.last),
            slave.r.ready.eq(rd_busy_r & ~rd_valid_r),
            master.r.valid.eq(rd_valid_r),
            master.r.data.eq(rd_data_r),
            master.r.resp.eq(rd_resp_r),
            master.r.first.eq(rd_first_r),
            master.r.last.eq(rd_last_r),
        ]

        self.sync += [
            If(master.ar.valid & master.ar.ready,
                rd_busy_r.eq(1),
                rd_word_r.eq(master.ar.addr[master_align:slave_align]),
            ),
            If(slave.r.valid & slave.r.ready,
                rd_valid_r.eq(1),
                rd_data_r.eq(selected_rdata),
                rd_resp_r.eq(slave.r.resp),
                rd_first_r.eq(slave.r.first),
                rd_last_r.eq(slave.r.last),
            ),
            If(master.r.valid & master.r.ready,
                rd_busy_r.eq(0),
                rd_valid_r.eq(0),
            ),
        ]


class AXIWritePayloadPipeline(Module):
    """Register full-width AXI write channels, but leave reads untouched.

    AXILiteAtomicUpConverter registers the selected read response.  Write
    channels retain their independent timing cut close to HBM.
    """
    def __init__(self, sink, source):
        self.submodules.aw_pipe = aw_pipe = stream.PipeValid(sink.aw.description)
        self.submodules.w_pipe  = w_pipe  = stream.PipeValid(sink.w.description)
        self.submodules.b_pipe  = b_pipe  = stream.PipeValid(source.b.description)

        self.comb += [
            sink.aw.connect(aw_pipe.sink),
            aw_pipe.source.connect(source.aw),
            sink.w.connect(w_pipe.sink),
            w_pipe.source.connect(source.w),
            source.b.connect(b_pipe.sink),
            b_pipe.source.connect(sink.b),
            sink.ar.connect(source.ar),
            source.r.connect(sink.r),
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
            ident="LiteX SoC on U280 + 24-port CFU AXI->HBM (four-context GRACE-KEM)", **kwargs)
        self.add_constant("CSR_HBM_8P64M_MAP")
        self.add_constant("CSR_HBM_20P64M_FROM8P_MAP")
        self.add_constant("CSR_HBM_PCMAP_LOCAL")
        self.add_constant("CFU_PORT_COUNT", CFU_PORT_COUNT)
        self.add_constant("CFU_DENSE_PORT_COUNT", CFU_DENSE_PORT_COUNT)
        self.add_constant("CFU_SPARSE_PORT_COUNT", CFU_SPARSE_PORT_COUNT)
        self.add_constant("CFU_COMPUTE0_PHYSICAL_PC", COMPUTE0_PHYSICAL_PC)

        # Add CFU Verilog include path
        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfuaxi_24p_kem4] Added Verilog include path: {incdir}")
        else:
            print("[cfuaxi_24p_kem4] WARNING: --cpu-cfu not provided; cannot add include path")

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

            # Split address map:
            #   hbm0 at 0x4000_0000 (64 MiB, cached): firmware / main_ram only.
            #   hbm1-20 at 0x8000_0000 + (pc-1)*0x0400_0000 (64 MiB, non-cached).
            # Logical CFU ports map to physical PCs through COMPUTE_PCS.
            cpu_axim = []
            for i in range(21):
                # Keep the bus-facing interface at the VexRiscv's native
                # width so the read-response timing register is downstream of
                # 256-to-32-bit lane selection.  An explicit converter avoids
                # LiteX inserting that selector on the wrong side of the
                # register when add_slave() adapts widths automatically.
                axi_lite = AXILiteInterface(data_width=32, address_width=32)
                axi_lite_wide = AXILiteInterface(data_width=256, address_width=33)
                axi_full_raw = AXIInterface(data_width=256, address_width=33, id_width=4)
                axi_full = AXIInterface(data_width=256, address_width=33, id_width=4)
                self.submodules += AXILiteAtomicUpConverter(
                    axi_lite, axi_lite_wide)
                self.submodules += AXILite2AXI(axi_lite_wide, axi_full_raw)
                self.submodules += AXIWritePayloadPipeline(axi_full_raw, axi_full)
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
            cpu_setup_lite = AXILiteInterface(data_width=32, address_width=32)
            cpu_setup_lite_wide = AXILiteInterface(data_width=256, address_width=33)
            cpu_setup_full_raw = AXIInterface(data_width=256, address_width=33, id_width=4)
            cpu_setup_full = AXIInterface(data_width=256, address_width=33, id_width=4)
            self.submodules += AXILiteAtomicUpConverter(
                cpu_setup_lite, cpu_setup_lite_wide)
            self.submodules += AXILite2AXI(cpu_setup_lite_wide, cpu_setup_full_raw)
            self.submodules += AXIWritePayloadPipeline(
                cpu_setup_full_raw, cpu_setup_full)
            self.bus.add_slave("hbm_setup", cpu_setup_lite,
                SoCRegion(origin=CPU_SETUP_BASE, size=WINDOW_BYTES, cached=False))
            # Only private ML-KEM contexts 1--3 need the selectable aperture;
            # context 0 and the ACCESS engines have fixed CPU windows.  Adding
            # all 24 PCs here created an unnecessary 24-way R-channel mux and
            # broad address/control fanout in the CPU critical cone.
            setup_slaves = []
            for pc in MLKEM_SETUP_PCS:
                selected = AXIInterface(data_width=256, address_width=33, id_width=4)
                # The final per-PC remap below keeps the aperture offset and
                # supplies the physical PC bits, so an intermediate arithmetic
                # rebase is neither needed nor desirable for timing.
                hbm_inputs[pc].append(selected)
                setup_slaves.append(
                    (lambda _, p=pc: port_map.cpu_setup_port.storage == p, selected)
                )
            self.submodules += AXIDecoder(cpu_setup_full, setup_slaves)

            # Twenty-four CFU AXI masters. Ports 0-18 retain the ACCESS
            # engines, port 19 is ML-KEM context 0, port 20 is the retained
            # GRACE DMA, and ports 21-23 are ML-KEM contexts 1-3.
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
                self.cpu.cfu_axi21,
                self.cpu.cfu_axi22,
                self.cpu.cfu_axi23,
                self.cpu.cfu_axi24,
            ]
            assert len(cfu_axim) == CFU_PORT_COUNT
            for port, master in enumerate(cfu_axim):
                hbm_inputs[logical_to_physical_pc(port)].append(master)

            # CPU and compute ports stay in their local 64 MiB windows.
            # PC21 is dedicated to DMA and must retain cross-PC addresses.
            assert hbm_inputs[GRACE_DMA_PC] == [self.cpu.cfu_axi21]
            for pc, inputs in enumerate(hbm_inputs):
                if inputs:
                    arb_out = AXIInterface(data_width=256, address_width=33, id_width=4)
                    self.submodules += AXIArbiter(inputs, arb_out)
                    self.submodules += AXIHBMPortRebase(arb_out, hbm.axi[pc], pc)

            # Keep the established ACCESS datapaths and every HBM-facing
            # bridge in SLR0.  The three replicated Keccak datapaths account
            # for roughly 10.5k LUTs and do not need direct HBM wiring; place
            # them in the first clock-region row of SLR1.  Their interface to
            # each local bridge is narrow and registered.  Selecting primitive
            # cells here is deliberate: assigning the Cfu hierarchy itself
            # would pull the excluded Keccak descendants back into pblock_cfu.
            platform.add_platform_command(
                "create_pblock pblock_cfu\n"
                "add_cells_to_pblock pblock_cfu [get_cells -hier -filter "
                "{{IS_PRIMITIVE && NAME =~ *Cfu* && "
                "NAME !~ *Cfu/u_mlkem_context1/u_keccak_boundary* && "
                "NAME !~ *Cfu/u_mlkem_context2/u_keccak_boundary* && "
                "NAME !~ *Cfu/u_mlkem_context3/u_keccak_boundary*}}]\n"
                "resize_pblock pblock_cfu -add CLOCKREGION_X0Y0:CLOCKREGION_X7Y3"
            )
            platform.add_platform_command(
                "create_pblock pblock_kem_parallel_keccak\n"
                "add_cells_to_pblock pblock_kem_parallel_keccak [get_cells -hier -filter "
                "{{IS_PRIMITIVE && ("
                "NAME =~ *Cfu/u_mlkem_context1/u_keccak_boundary* || "
                "NAME =~ *Cfu/u_mlkem_context2/u_keccak_boundary* || "
                "NAME =~ *Cfu/u_mlkem_context3/u_keccak_boundary*)}}]\n"
                "resize_pblock pblock_kem_parallel_keccak -add "
                "CLOCKREGION_X0Y4:CLOCKREGION_X7Y4"
            )

            # W5: keep the CPU (and the flattened Wishbone<->AXI converter
            # logic that lands under its hierarchy) near the HBM interface
            # columns at the bottom of SLR0 — the -1.6 ns paths were 77%
            # route delay between HBM hard-macro pins and scattered CPU cells.
            platform.add_platform_command(
                "create_pblock pblock_cpu\n"
                "add_cells_to_pblock pblock_cpu [get_cells -hier -filter {{NAME =~ VexRiscv*}}]\n"
                "resize_pblock pblock_cpu -add CLOCKREGION_X2Y0:CLOCKREGION_X5Y1"
            )

            # W5: timing-focused implementation directives (LiteX vivado
            # backend attributes; VecMADS closes 260 MHz with this class of
            # directive sweep).
            platform.toolchain.vivado_place_directive               = "ExtraTimingOpt"
            platform.toolchain.vivado_post_place_phys_opt_directive = "AggressiveExplore"
            platform.toolchain.vivado_route_directive               = "AggressiveExplore"
            # W5 Result 4 verdict: post-route phys_opt AggressiveExplore cost
            # 3x build time (11h18m vs 3h40m) for +0.007ns WNS — noise. Off by
            # default; opt in with W5_POST_ROUTE_PHYS_OPT=1 (not recommended
            # even for final builds — prefer a seed/place-directive variation
            # on the cheap config). `os` is imported at module level; a local
            # import here would shadow it for the whole function scope.
            if os.environ.get("W5_POST_ROUTE_PHYS_OPT", "") == "1":
                platform.toolchain.vivado_post_route_phys_opt_directive = "AggressiveExplore"

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
    parser = LiteXSoCArgumentParser(description="U280 + 24-port CFU AXI->HBM for four-context GRACE-KEM")
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
