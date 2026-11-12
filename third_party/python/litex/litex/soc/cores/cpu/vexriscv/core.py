#
# This file is part of LiteX.
#
# Copyright (c) 2018 Dolu1990 <charles.papon.90@gmail.com>
# Copyright (c) 2018-2019 Florent Kermarrec <florent@enjoy-digital.fr>
# Copyright (c) 2018-2019 Sean Cross <sean@xobs.io>
# Copyright (c) 2019 Tim 'mithro' Ansell <me@mith.ro>
# Copyright (c) 2019 David Shah <dave@ds0.me>
# Copyright (c) 2019 Antmicro <www.antmicro.com>
# Copyright (c) 2019 Kurt Kiefer <kekiefer@gmail.com>
# SPDX-License-Identifier: BSD-2-Clause

import os

from migen import *

from litex import get_data_mod
from litex.soc.interconnect import wishbone
from litex.soc.interconnect.csr import *
from litex.soc.cores.cpu import CPU, CPU_GCC_TRIPLE_RISCV32
from litex.soc.interconnect.axi import AXIInterface                     # added this 

# Variants -----------------------------------------------------------------------------------------

CPU_VARIANTS = {
    "minimal":            "VexRiscv_Min",
    "minimal+debug":      "VexRiscv_MinDebug",
    "minimal+debug+hwbp": "VexRiscv_MinDebugHwBP",
    "lite":               "VexRiscv_Lite",
    "lite+debug":         "VexRiscv_LiteDebug",
    "lite+debug+hwbp":    "VexRiscv_LiteDebugHwBP",
    "standard":           "VexRiscv",
    "standard+debug":     "VexRiscv_Debug",
    "imac":               "VexRiscv_IMAC",
    "imac+debug":         "VexRiscv_IMACDebug",
    "full":               "VexRiscv_Full",
    "full+cfu":           "VexRiscv_FullCfu",
    "full+debug":         "VexRiscv_FullDebug",
    "full+cfu+debug":     "VexRiscv_FullCfuDebug",
    "linux":              "VexRiscv_Linux",
    "linux+debug":        "VexRiscv_LinuxDebug",
    "linux+no-dsp":       "VexRiscv_LinuxNoDspFmax",
    "secure":             "VexRiscv_Secure",
    "secure+debug":       "VexRiscv_SecureDebug",
}

# GCC Flags ----------------------------------------------------------------------------------------

GCC_FLAGS = {
    #                               /---------- Base ISA
    #                               |    /----- Hardware Multiply + Divide
    #                               |    |/---- Atomics
    #                               |    ||/--- Compressed ISA
    #                               |    |||/-- Single-Precision Floating-Point
    #                               |    ||||/- Double-Precision Floating-Point
    #                               i    macfd
    "minimal":          "-march=rv32i2p0       -mabi=ilp32",
    "minimal+debug":    "-march=rv32i2p0       -mabi=ilp32",
    "lite":             "-march=rv32i2p0_m     -mabi=ilp32",
    "lite+debug":       "-march=rv32i2p0_m     -mabi=ilp32",
    "standard":         "-march=rv32i2p0_m     -mabi=ilp32",
    "standard+debug":   "-march=rv32i2p0_m     -mabi=ilp32",
    "imac":             "-march=rv32i2p0_mac   -mabi=ilp32",
    "imac+debug":       "-march=rv32i2p0_mac   -mabi=ilp32",
    "full":             "-march=rv32i2p0_m     -mabi=ilp32",
    "full+cfu":         "-march=rv32i2p0_m     -mabi=ilp32",
    "full+debug":       "-march=rv32i2p0_m     -mabi=ilp32",
    "full+cfu+debug":   "-march=rv32i2p0_m     -mabi=ilp32",
    "linux":            "-march=rv32i2p0_ma    -mabi=ilp32",
    "linux+debug":      "-march=rv32i2p0_ma    -mabi=ilp32",
    "linux+no-dsp":     "-march=rv32i2p0_ma    -mabi=ilp32",
    "secure":           "-march=rv32i2p0_ma    -mabi=ilp32",
    "secure+debug":     "-march=rv32i2p0_ma    -mabi=ilp32",
}

# VexRiscv Timer -----------------------------------------------------------------------------------

class VexRiscvTimer(Module, AutoCSR):
    def __init__(self):
        self._latch    = CSR()
        self._time     = CSRStatus(64)
        self._time_cmp = CSRStorage(64, reset=2**64-1)
        self.interrupt = Signal()

        # # #

        time = Signal(64)
        self.sync += time.eq(time + 1)
        self.sync += If(self._latch.re, self._time.status.eq(time))

        time_cmp = Signal(64, reset=2**64-1)
        self.sync += If(self._latch.re, time_cmp.eq(self._time_cmp.storage))

        self.comb += self.interrupt.eq(time >= time_cmp)

# VexRiscv -----------------------------------------------------------------------------------------

class VexRiscv(CPU, AutoCSR):
    category             = "softcore"
    family               = "riscv"
    name                 = "vexriscv"
    human_name           = "VexRiscv"
    variants             = CPU_VARIANTS
    data_width           = 32
    endianness           = "little"
    gcc_triple           = CPU_GCC_TRIPLE_RISCV32
    linker_output_format = "elf32-littleriscv"
    nop                  = "nop"
    io_regions           = {0x8000_0000: 0x8000_0000} # Origin, Length

    # Memory Mapping.
    @property
    def mem_map(self):
        return {
            "rom":            0x0000_0000,
            "sram":           0x1000_0000,
            "main_ram":       0x4000_0000,
            "csr":            0xf000_0000,
            "vexriscv_debug": 0xf00f_0000,
        }

    # GCC Flags.
    @property
    def gcc_flags(self):
        flags = GCC_FLAGS[self.variant]
        flags += " -D__vexriscv__"
        return flags

    def __init__(self, platform, variant="standard", with_timer=False):
        self.platform         = platform
        self.variant          = variant
        self.human_name       = CPU_VARIANTS.get(variant, "VexRiscv")
        self.external_variant = None
        self.reset            = Signal()
        self.interrupt        = Signal(32)
        self.ibus             = ibus = wishbone.Interface()
        self.dbus             = dbus = wishbone.Interface()
        self.periph_buses     = [ibus, dbus] # Peripheral buses (Connected to main SoC's bus).
        self.memory_buses     = []           # Memory buses (Connected directly to LiteDRAM).

        # # #

        # CPU Instance.
        self.cpu_params = dict(
            i_clk                    = ClockSignal("sys"),
            i_reset                  = ResetSignal("sys") | self.reset,

            i_externalInterruptArray = self.interrupt,
            i_timerInterrupt         = 0,
            i_softwareInterrupt      = 0,

            o_iBusWishbone_ADR      = ibus.adr,
            o_iBusWishbone_DAT_MOSI = ibus.dat_w,
            o_iBusWishbone_SEL      = ibus.sel,
            o_iBusWishbone_CYC      = ibus.cyc,
            o_iBusWishbone_STB      = ibus.stb,
            o_iBusWishbone_WE       = ibus.we,
            o_iBusWishbone_CTI      = ibus.cti,
            o_iBusWishbone_BTE      = ibus.bte,
            i_iBusWishbone_DAT_MISO = ibus.dat_r,
            i_iBusWishbone_ACK      = ibus.ack,
            i_iBusWishbone_ERR      = ibus.err,

            o_dBusWishbone_ADR      = dbus.adr,
            o_dBusWishbone_DAT_MOSI = dbus.dat_w,
            o_dBusWishbone_SEL      = dbus.sel,
            o_dBusWishbone_CYC      = dbus.cyc,
            o_dBusWishbone_STB      = dbus.stb,
            o_dBusWishbone_WE       = dbus.we,
            o_dBusWishbone_CTI      = dbus.cti,
            o_dBusWishbone_BTE      = dbus.bte,
            i_dBusWishbone_DAT_MISO = dbus.dat_r,
            i_dBusWishbone_ACK      = dbus.ack,
            i_dBusWishbone_ERR      = dbus.err
        )

        # Add Timer (Optional).
        if with_timer:
            self.add_timer()

        # Add Debug (Optional).
        if "debug" in variant:
            self.add_debug()

    def set_reset_address(self, reset_address):
        self.reset_address = reset_address
        self.cpu_params.update(i_externalResetVector=Signal(32, reset=reset_address))

    def add_timer(self):
        self.submodules.timer = VexRiscvTimer()
        self.cpu_params.update(i_timerInterrupt=self.timer.interrupt)

    def add_debug(self):
        debug_reset = Signal()

        ibus_err = Signal()
        dbus_err = Signal()

        self.i_cmd_valid           = Signal()
        self.i_cmd_payload_wr      = Signal()
        self.i_cmd_payload_address = Signal(8)
        self.i_cmd_payload_data    = Signal(32)
        self.o_cmd_ready           = Signal()
        self.o_rsp_data            = Signal(32)
        self.o_resetOut            = Signal()

        reset_debug_logic = Signal()

        self.transfer_complete     = Signal()
        self.transfer_in_progress  = Signal()
        self.transfer_wait_for_ack = Signal()

        self.debug_bus = wishbone.Interface()

        self.sync += self.debug_bus.dat_r.eq(self.o_rsp_data)
        self.sync += debug_reset.eq(reset_debug_logic | ResetSignal())

        self.sync += [
            # CYC is held high for the duration of the transfer.
            # STB is kept high when the transfer finishes (write)
            # or the master is waiting for data (read), and stays
            # there until ACK, ERR, or RTY are asserted.
            If((self.debug_bus.stb & self.debug_bus.cyc)
            & (~self.transfer_in_progress)
            & (~self.transfer_complete)
            & (~self.transfer_wait_for_ack),
                self.i_cmd_payload_data.eq(self.debug_bus.dat_w),
                self.i_cmd_payload_address.eq((self.debug_bus.adr[0:6] << 2) | 0),
                self.i_cmd_payload_wr.eq(self.debug_bus.we),
                self.i_cmd_valid.eq(1),
                self.transfer_in_progress.eq(1),
                self.transfer_complete.eq(0),
                self.debug_bus.ack.eq(0)
            ).Elif(self.transfer_in_progress,
                If(self.o_cmd_ready,
                    self.i_cmd_valid.eq(0),
                    self.i_cmd_payload_wr.eq(0),
                    self.transfer_complete.eq(1),
                    self.transfer_in_progress.eq(0)
                )
            ).Elif(self.transfer_complete,
                self.transfer_complete.eq(0),
                self.debug_bus.ack.eq(1),
                self.transfer_wait_for_ack.eq(1)
            ).Elif(self.transfer_wait_for_ack & ~(self.debug_bus.stb & self.debug_bus.cyc),
                self.transfer_wait_for_ack.eq(0),
                self.debug_bus.ack.eq(0)
            ),
            # Force a Wishbone error if transferring during a reset sequence.
            # Because o_resetOut is multiple cycles and i.stb/d.stb should
            # deassert one cycle after i_err/i_ack/d_err/d_ack are asserted,
            # this will give i_err and o_err enough time to be reset to 0
            # once the reset cycle finishes.
            If(self.o_resetOut,
                If(self.ibus.cyc & self.ibus.stb, ibus_err.eq(1)).Else(ibus_err.eq(0)),
                If(self.dbus.cyc & self.dbus.stb, dbus_err.eq(1)).Else(dbus_err.eq(0)),
                reset_debug_logic.eq(1))
            .Else(
                reset_debug_logic.eq(0)
            )
        ]

        self.cpu_params.update(
            i_reset = ResetSignal() | self.reset | debug_reset,
            i_iBusWishbone_ERR              = self.ibus.err | ibus_err,
            i_dBusWishbone_ERR              = self.dbus.err | dbus_err,
            i_debugReset                    = ResetSignal(),
            i_debug_bus_cmd_valid           = self.i_cmd_valid,
            i_debug_bus_cmd_payload_wr      = self.i_cmd_payload_wr,
            i_debug_bus_cmd_payload_address = self.i_cmd_payload_address,
            i_debug_bus_cmd_payload_data    = self.i_cmd_payload_data,
            o_debug_bus_cmd_ready           = self.o_cmd_ready,
            o_debug_bus_rsp_data            = self.o_rsp_data,
            o_debug_resetOut                = self.o_resetOut
        )

    def add_cfu(self, cfu_filename):
        # Check CFU presence.
        if not os.path.exists(cfu_filename):
            raise OSError(f"Unable to find VexRiscv CFU plugin {cfu_filename}.")

        # CFU:CPU Bus Layout.
        cfu_bus_layout = [
            ("cmd", [
                ("valid", 1),
                ("ready", 1),
                ("payload", [
                    ("function_id", 10),
                    ("inputs_0", 32),
                    ("inputs_1", 32),
                ]),
            ]),
            ("rsp", [
                ("valid", 1),
                ("ready", 1),
                ("payload", [
                    ("outputs_0", 32),
                ]),
            ]),
        ]

        # The CFU:CPU Bus.
        self.cfu_bus = cfu_bus = Record(cfu_bus_layout)

        # Connect CFU to the CFU:CPU bus.
        self.cfu_params = dict(
            i_cmd_valid                = cfu_bus.cmd.valid,
            o_cmd_ready                = cfu_bus.cmd.ready,
            i_cmd_payload_function_id  = cfu_bus.cmd.payload.function_id,
            i_cmd_payload_inputs_0     = cfu_bus.cmd.payload.inputs_0,
            i_cmd_payload_inputs_1     = cfu_bus.cmd.payload.inputs_1,
            o_rsp_valid                = cfu_bus.rsp.valid,
            i_rsp_ready                = cfu_bus.rsp.ready,
            o_rsp_payload_outputs_0    = cfu_bus.rsp.payload.outputs_0,
            i_clk                      = ClockSignal("sys"),
            i_reset                    = ResetSignal("sys"),
        )
        self.platform.add_source(cfu_filename)

        # Connect CPU to the CFU:CPU bus.
        self.cpu_params.update(
            o_CfuPlugin_bus_cmd_valid                = cfu_bus.cmd.valid,
            i_CfuPlugin_bus_cmd_ready                = cfu_bus.cmd.ready,
            o_CfuPlugin_bus_cmd_payload_function_id  = cfu_bus.cmd.payload.function_id,
            o_CfuPlugin_bus_cmd_payload_inputs_0     = cfu_bus.cmd.payload.inputs_0,
            o_CfuPlugin_bus_cmd_payload_inputs_1     = cfu_bus.cmd.payload.inputs_1,
            i_CfuPlugin_bus_rsp_valid                = cfu_bus.rsp.valid,
            o_CfuPlugin_bus_rsp_ready                = cfu_bus.rsp.ready,
            i_CfuPlugin_bus_rsp_payload_outputs_0    = cfu_bus.rsp.payload.outputs_0,
        )

        # ---------- NEW: expose CFU's AXI master as a LiteX AXIInterface ----------                # added
        self.cfu_axi = AXIInterface(data_width=256, address_width=33, id_width=4)
        m = self.cfu_axi
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi_awid"    : m.aw.id,
            "o_m_axi_awaddr"  : m.aw.addr,
            "o_m_axi_awlen"   : m.aw.len,
            "o_m_axi_awsize"  : m.aw.size,
            "o_m_axi_awburst" : m.aw.burst,
            "o_m_axi_awvalid" : m.aw.valid,
            "i_m_axi_awready" : m.aw.ready,

            # WRITE DATA
            "o_m_axi_wdata"   : m.w.data,
            "o_m_axi_wstrb"   : m.w.strb,
            "o_m_axi_wlast"   : m.w.last,
            "o_m_axi_wvalid"  : m.w.valid,
            "i_m_axi_wready"  : m.w.ready,

            # WRITE RESP
            "i_m_axi_bid"     : m.b.id,
            "i_m_axi_bresp"   : m.b.resp,
            "i_m_axi_bvalid"  : m.b.valid,
            "o_m_axi_bready"  : m.b.ready,

            # READ ADDRESS
            "o_m_axi_arid"    : m.ar.id,
            "o_m_axi_araddr"  : m.ar.addr,
            "o_m_axi_arlen"   : m.ar.len,
            "o_m_axi_arsize"  : m.ar.size,
            "o_m_axi_arburst" : m.ar.burst,
            "o_m_axi_arvalid" : m.ar.valid,
            "i_m_axi_arready" : m.ar.ready,

            # READ DATA
            "i_m_axi_rid"     : m.r.id,
            "i_m_axi_rdata"   : m.r.data,
            "i_m_axi_rresp"   : m.r.resp,
            "i_m_axi_rlast"   : m.r.last,
            "i_m_axi_rvalid"  : m.r.valid,
            "o_m_axi_rready"  : m.r.ready,
        })

        self.cfu_axi2 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m2 = self.cfu_axi2
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi2_awid"    : m2.aw.id,
            "o_m_axi2_awaddr"  : m2.aw.addr,
            "o_m_axi2_awlen"   : m2.aw.len,
            "o_m_axi2_awsize"  : m2.aw.size,
            "o_m_axi2_awburst" : m2.aw.burst,
            "o_m_axi2_awvalid" : m2.aw.valid,
            "i_m_axi2_awready" : m2.aw.ready,

            # WRITE DATA
            "o_m_axi2_wdata"   : m2.w.data,
            "o_m_axi2_wstrb"   : m2.w.strb,
            "o_m_axi2_wlast"   : m2.w.last,
            "o_m_axi2_wvalid"  : m2.w.valid,
            "i_m_axi2_wready"  : m2.w.ready,

            # WRITE RESP
            "i_m_axi2_bid"     : m2.b.id,
            "i_m_axi2_bresp"   : m2.b.resp,
            "i_m_axi2_bvalid"  : m2.b.valid,
            "o_m_axi2_bready"  : m2.b.ready,

            # READ ADDRESS
            "o_m_axi2_arid"    : m2.ar.id,
            "o_m_axi2_araddr"  : m2.ar.addr,
            "o_m_axi2_arlen"   : m2.ar.len,
            "o_m_axi2_arsize"  : m2.ar.size,
            "o_m_axi2_arburst" : m2.ar.burst,
            "o_m_axi2_arvalid" : m2.ar.valid,
            "i_m_axi2_arready" : m2.ar.ready,

            # READ DATA
            "i_m_axi2_rid"     : m2.r.id,
            "i_m_axi2_rdata"   : m2.r.data,
            "i_m_axi2_rresp"   : m2.r.resp,
            "i_m_axi2_rlast"   : m2.r.last,
            "i_m_axi2_rvalid"  : m2.r.valid,
            "o_m_axi2_rready"  : m2.r.ready,
        })

        self.cfu_axi3 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m3 = self.cfu_axi3
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi3_awid"    : m3.aw.id,
            "o_m_axi3_awaddr"  : m3.aw.addr,
            "o_m_axi3_awlen"   : m3.aw.len,
            "o_m_axi3_awsize"  : m3.aw.size,
            "o_m_axi3_awburst" : m3.aw.burst,
            "o_m_axi3_awvalid" : m3.aw.valid,
            "i_m_axi3_awready" : m3.aw.ready,

            # WRITE DATA
            "o_m_axi3_wdata"   : m3.w.data,
            "o_m_axi3_wstrb"   : m3.w.strb,
            "o_m_axi3_wlast"   : m3.w.last,
            "o_m_axi3_wvalid"  : m3.w.valid,
            "i_m_axi3_wready"  : m3.w.ready,

            # WRITE RESP
            "i_m_axi3_bid"     : m3.b.id,
            "i_m_axi3_bresp"   : m3.b.resp,
            "i_m_axi3_bvalid"  : m3.b.valid,
            "o_m_axi3_bready"  : m3.b.ready,

            # READ ADDRESS
            "o_m_axi3_arid"    : m3.ar.id,
            "o_m_axi3_araddr"  : m3.ar.addr,
            "o_m_axi3_arlen"   : m3.ar.len,
            "o_m_axi3_arsize"  : m3.ar.size,
            "o_m_axi3_arburst" : m3.ar.burst,
            "o_m_axi3_arvalid" : m3.ar.valid,
            "i_m_axi3_arready" : m3.ar.ready,

            # READ DATA
            "i_m_axi3_rid"     : m3.r.id,
            "i_m_axi3_rdata"   : m3.r.data,
            "i_m_axi3_rresp"   : m3.r.resp,
            "i_m_axi3_rlast"   : m3.r.last,
            "i_m_axi3_rvalid"  : m3.r.valid,
            "o_m_axi3_rready"  : m3.r.ready,
        })

        self.cfu_axi4 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m4 = self.cfu_axi4
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi4_awid"    : m4.aw.id,
            "o_m_axi4_awaddr"  : m4.aw.addr,
            "o_m_axi4_awlen"   : m4.aw.len,
            "o_m_axi4_awsize"  : m4.aw.size,
            "o_m_axi4_awburst" : m4.aw.burst,
            "o_m_axi4_awvalid" : m4.aw.valid,
            "i_m_axi4_awready" : m4.aw.ready,

            # WRITE DATA
            "o_m_axi4_wdata"   : m4.w.data,
            "o_m_axi4_wstrb"   : m4.w.strb,
            "o_m_axi4_wlast"   : m4.w.last,
            "o_m_axi4_wvalid"  : m4.w.valid,
            "i_m_axi4_wready"  : m4.w.ready,

            # WRITE RESP
            "i_m_axi4_bid"     : m4.b.id,
            "i_m_axi4_bresp"   : m4.b.resp,
            "i_m_axi4_bvalid"  : m4.b.valid,
            "o_m_axi4_bready"  : m4.b.ready,

            # READ ADDRESS
            "o_m_axi4_arid"    : m4.ar.id,
            "o_m_axi4_araddr"  : m4.ar.addr,
            "o_m_axi4_arlen"   : m4.ar.len,
            "o_m_axi4_arsize"  : m4.ar.size,
            "o_m_axi4_arburst" : m4.ar.burst,
            "o_m_axi4_arvalid" : m4.ar.valid,
            "i_m_axi4_arready" : m4.ar.ready,

            # READ DATA
            "i_m_axi4_rid"     : m4.r.id,
            "i_m_axi4_rdata"   : m4.r.data,
            "i_m_axi4_rresp"   : m4.r.resp,
            "i_m_axi4_rlast"   : m4.r.last,
            "i_m_axi4_rvalid"  : m4.r.valid,
            "o_m_axi4_rready"  : m4.r.ready,
        })

        self.cfu_axi5 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m5 = self.cfu_axi5
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi5_awid"    : m5.aw.id,
            "o_m_axi5_awaddr"  : m5.aw.addr,
            "o_m_axi5_awlen"   : m5.aw.len,
            "o_m_axi5_awsize"  : m5.aw.size,
            "o_m_axi5_awburst" : m5.aw.burst,
            "o_m_axi5_awvalid" : m5.aw.valid,
            "i_m_axi5_awready" : m5.aw.ready,

            # WRITE DATA
            "o_m_axi5_wdata"   : m5.w.data,
            "o_m_axi5_wstrb"   : m5.w.strb,
            "o_m_axi5_wlast"   : m5.w.last,
            "o_m_axi5_wvalid"  : m5.w.valid,
            "i_m_axi5_wready"  : m5.w.ready,

            # WRITE RESP
            "i_m_axi5_bid"     : m5.b.id,
            "i_m_axi5_bresp"   : m5.b.resp,
            "i_m_axi5_bvalid"  : m5.b.valid,
            "o_m_axi5_bready"  : m5.b.ready,

            # READ ADDRESS
            "o_m_axi5_arid"    : m5.ar.id,
            "o_m_axi5_araddr"  : m5.ar.addr,
            "o_m_axi5_arlen"   : m5.ar.len,
            "o_m_axi5_arsize"  : m5.ar.size,
            "o_m_axi5_arburst" : m5.ar.burst,
            "o_m_axi5_arvalid" : m5.ar.valid,
            "i_m_axi5_arready" : m5.ar.ready,

            # READ DATA
            "i_m_axi5_rid"     : m5.r.id,
            "i_m_axi5_rdata"   : m5.r.data,
            "i_m_axi5_rresp"   : m5.r.resp,
            "i_m_axi5_rlast"   : m5.r.last,
            "i_m_axi5_rvalid"  : m5.r.valid,
            "o_m_axi5_rready"  : m5.r.ready,
        })

        self.cfu_axi6 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m6 = self.cfu_axi6
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi6_awid"    : m6.aw.id,
            "o_m_axi6_awaddr"  : m6.aw.addr,
            "o_m_axi6_awlen"   : m6.aw.len,
            "o_m_axi6_awsize"  : m6.aw.size,
            "o_m_axi6_awburst" : m6.aw.burst,
            "o_m_axi6_awvalid" : m6.aw.valid,
            "i_m_axi6_awready" : m6.aw.ready,

            # WRITE DATA
            "o_m_axi6_wdata"   : m6.w.data,
            "o_m_axi6_wstrb"   : m6.w.strb,
            "o_m_axi6_wlast"   : m6.w.last,
            "o_m_axi6_wvalid"  : m6.w.valid,
            "i_m_axi6_wready"  : m6.w.ready,

            # WRITE RESP
            "i_m_axi6_bid"     : m6.b.id,
            "i_m_axi6_bresp"   : m6.b.resp,
            "i_m_axi6_bvalid"  : m6.b.valid,
            "o_m_axi6_bready"  : m6.b.ready,

            # READ ADDRESS
            "o_m_axi6_arid"    : m6.ar.id,
            "o_m_axi6_araddr"  : m6.ar.addr,
            "o_m_axi6_arlen"   : m6.ar.len,
            "o_m_axi6_arsize"  : m6.ar.size,
            "o_m_axi6_arburst" : m6.ar.burst,
            "o_m_axi6_arvalid" : m6.ar.valid,
            "i_m_axi6_arready" : m6.ar.ready,

            # READ DATA
            "i_m_axi6_rid"     : m6.r.id,
            "i_m_axi6_rdata"   : m6.r.data,
            "i_m_axi6_rresp"   : m6.r.resp,
            "i_m_axi6_rlast"   : m6.r.last,
            "i_m_axi6_rvalid"  : m6.r.valid,
            "o_m_axi6_rready"  : m6.r.ready,
        })

        self.cfu_axi7 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m7 = self.cfu_axi7
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi7_awid"    : m7.aw.id,
            "o_m_axi7_awaddr"  : m7.aw.addr,
            "o_m_axi7_awlen"   : m7.aw.len,
            "o_m_axi7_awsize"  : m7.aw.size,
            "o_m_axi7_awburst" : m7.aw.burst,
            "o_m_axi7_awvalid" : m7.aw.valid,
            "i_m_axi7_awready" : m7.aw.ready,

            # WRITE DATA
            "o_m_axi7_wdata"   : m7.w.data,
            "o_m_axi7_wstrb"   : m7.w.strb,
            "o_m_axi7_wlast"   : m7.w.last,
            "o_m_axi7_wvalid"  : m7.w.valid,
            "i_m_axi7_wready"  : m7.w.ready,

            # WRITE RESP
            "i_m_axi7_bid"     : m7.b.id,
            "i_m_axi7_bresp"   : m7.b.resp,
            "i_m_axi7_bvalid"  : m7.b.valid,
            "o_m_axi7_bready"  : m7.b.ready,

            # READ ADDRESS
            "o_m_axi7_arid"    : m7.ar.id,
            "o_m_axi7_araddr"  : m7.ar.addr,
            "o_m_axi7_arlen"   : m7.ar.len,
            "o_m_axi7_arsize"  : m7.ar.size,
            "o_m_axi7_arburst" : m7.ar.burst,
            "o_m_axi7_arvalid" : m7.ar.valid,
            "i_m_axi7_arready" : m7.ar.ready,

            # READ DATA
            "i_m_axi7_rid"     : m7.r.id,
            "i_m_axi7_rdata"   : m7.r.data,
            "i_m_axi7_rresp"   : m7.r.resp,
            "i_m_axi7_rlast"   : m7.r.last,
            "i_m_axi7_rvalid"  : m7.r.valid,
            "o_m_axi7_rready"  : m7.r.ready,
        })

        self.cfu_axi8 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m8 = self.cfu_axi8
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi8_awid"    : m8.aw.id,
            "o_m_axi8_awaddr"  : m8.aw.addr,
            "o_m_axi8_awlen"   : m8.aw.len,
            "o_m_axi8_awsize"  : m8.aw.size,
            "o_m_axi8_awburst" : m8.aw.burst,
            "o_m_axi8_awvalid" : m8.aw.valid,
            "i_m_axi8_awready" : m8.aw.ready,

            # WRITE DATA
            "o_m_axi8_wdata"   : m8.w.data,
            "o_m_axi8_wstrb"   : m8.w.strb,
            "o_m_axi8_wlast"   : m8.w.last,
            "o_m_axi8_wvalid"  : m8.w.valid,
            "i_m_axi8_wready"  : m8.w.ready,

            # WRITE RESP
            "i_m_axi8_bid"     : m8.b.id,
            "i_m_axi8_bresp"   : m8.b.resp,
            "i_m_axi8_bvalid"  : m8.b.valid,
            "o_m_axi8_bready"  : m8.b.ready,

            # READ ADDRESS
            "o_m_axi8_arid"    : m8.ar.id,
            "o_m_axi8_araddr"  : m8.ar.addr,
            "o_m_axi8_arlen"   : m8.ar.len,
            "o_m_axi8_arsize"  : m8.ar.size,
            "o_m_axi8_arburst" : m8.ar.burst,
            "o_m_axi8_arvalid" : m8.ar.valid,
            "i_m_axi8_arready" : m8.ar.ready,

            # READ DATA
            "i_m_axi8_rid"     : m8.r.id,
            "i_m_axi8_rdata"   : m8.r.data,
            "i_m_axi8_rresp"   : m8.r.resp,
            "i_m_axi8_rlast"   : m8.r.last,
            "i_m_axi8_rvalid"  : m8.r.valid,
            "o_m_axi8_rready"  : m8.r.ready,
        })

        self.cfu_axi9 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m9 = self.cfu_axi9
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi9_awid"    : m9.aw.id,
            "o_m_axi9_awaddr"  : m9.aw.addr,
            "o_m_axi9_awlen"   : m9.aw.len,
            "o_m_axi9_awsize"  : m9.aw.size,
            "o_m_axi9_awburst" : m9.aw.burst,
            "o_m_axi9_awvalid" : m9.aw.valid,
            "i_m_axi9_awready" : m9.aw.ready,

            # WRITE DATA
            "o_m_axi9_wdata"   : m9.w.data,
            "o_m_axi9_wstrb"   : m9.w.strb,
            "o_m_axi9_wlast"   : m9.w.last,
            "o_m_axi9_wvalid"  : m9.w.valid,
            "i_m_axi9_wready"  : m9.w.ready,

            # WRITE RESP
            "i_m_axi9_bid"     : m9.b.id,
            "i_m_axi9_bresp"   : m9.b.resp,
            "i_m_axi9_bvalid"  : m9.b.valid,
            "o_m_axi9_bready"  : m9.b.ready,

            # READ ADDRESS
            "o_m_axi9_arid"    : m9.ar.id,
            "o_m_axi9_araddr"  : m9.ar.addr,
            "o_m_axi9_arlen"   : m9.ar.len,
            "o_m_axi9_arsize"  : m9.ar.size,
            "o_m_axi9_arburst" : m9.ar.burst,
            "o_m_axi9_arvalid" : m9.ar.valid,
            "i_m_axi9_arready" : m9.ar.ready,

            # READ DATA
            "i_m_axi9_rid"     : m9.r.id,
            "i_m_axi9_rdata"   : m9.r.data,
            "i_m_axi9_rresp"   : m9.r.resp,
            "i_m_axi9_rlast"   : m9.r.last,
            "i_m_axi9_rvalid"  : m9.r.valid,
            "o_m_axi9_rready"  : m9.r.ready,
        })

        self.cfu_axi10 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m10 = self.cfu_axi10
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi10_awid"    : m10.aw.id,
            "o_m_axi10_awaddr"  : m10.aw.addr,
            "o_m_axi10_awlen"   : m10.aw.len,
            "o_m_axi10_awsize"  : m10.aw.size,
            "o_m_axi10_awburst" : m10.aw.burst,
            "o_m_axi10_awvalid" : m10.aw.valid,
            "i_m_axi10_awready" : m10.aw.ready,

            # WRITE DATA
            "o_m_axi10_wdata"   : m10.w.data,
            "o_m_axi10_wstrb"   : m10.w.strb,
            "o_m_axi10_wlast"   : m10.w.last,
            "o_m_axi10_wvalid"  : m10.w.valid,
            "i_m_axi10_wready"  : m10.w.ready,

            # WRITE RESP
            "i_m_axi10_bid"     : m10.b.id,
            "i_m_axi10_bresp"   : m10.b.resp,
            "i_m_axi10_bvalid"  : m10.b.valid,
            "o_m_axi10_bready"  : m10.b.ready,

            # READ ADDRESS
            "o_m_axi10_arid"    : m10.ar.id,
            "o_m_axi10_araddr"  : m10.ar.addr,
            "o_m_axi10_arlen"   : m10.ar.len,
            "o_m_axi10_arsize"  : m10.ar.size,
            "o_m_axi10_arburst" : m10.ar.burst,
            "o_m_axi10_arvalid" : m10.ar.valid,
            "i_m_axi10_arready" : m10.ar.ready,

            # READ DATA
            "i_m_axi10_rid"     : m10.r.id,
            "i_m_axi10_rdata"   : m10.r.data,
            "i_m_axi10_rresp"   : m10.r.resp,
            "i_m_axi10_rlast"   : m10.r.last,
            "i_m_axi10_rvalid"  : m10.r.valid,
            "o_m_axi10_rready"  : m10.r.ready,
        })

        self.cfu_axi11 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m11 = self.cfu_axi11
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi11_awid"    : m11.aw.id,
            "o_m_axi11_awaddr"  : m11.aw.addr,
            "o_m_axi11_awlen"   : m11.aw.len,
            "o_m_axi11_awsize"  : m11.aw.size,
            "o_m_axi11_awburst" : m11.aw.burst,
            "o_m_axi11_awvalid" : m11.aw.valid,
            "i_m_axi11_awready" : m11.aw.ready,

            # WRITE DATA
            "o_m_axi11_wdata"   : m11.w.data,
            "o_m_axi11_wstrb"   : m11.w.strb,
            "o_m_axi11_wlast"   : m11.w.last,
            "o_m_axi11_wvalid"  : m11.w.valid,
            "i_m_axi11_wready"  : m11.w.ready,

            # WRITE RESP
            "i_m_axi11_bid"     : m11.b.id,
            "i_m_axi11_bresp"   : m11.b.resp,
            "i_m_axi11_bvalid"  : m11.b.valid,
            "o_m_axi11_bready"  : m11.b.ready,

            # READ ADDRESS
            "o_m_axi11_arid"    : m11.ar.id,
            "o_m_axi11_araddr"  : m11.ar.addr,
            "o_m_axi11_arlen"   : m11.ar.len,
            "o_m_axi11_arsize"  : m11.ar.size,
            "o_m_axi11_arburst" : m11.ar.burst,
            "o_m_axi11_arvalid" : m11.ar.valid,
            "i_m_axi11_arready" : m11.ar.ready,

            # READ DATA
            "i_m_axi11_rid"     : m11.r.id,
            "i_m_axi11_rdata"   : m11.r.data,
            "i_m_axi11_rresp"   : m11.r.resp,
            "i_m_axi11_rlast"   : m11.r.last,
            "i_m_axi11_rvalid"  : m11.r.valid,
            "o_m_axi11_rready"  : m11.r.ready,
        })

        self.cfu_axi12 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m12 = self.cfu_axi12
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi12_awid"    : m12.aw.id,
            "o_m_axi12_awaddr"  : m12.aw.addr,
            "o_m_axi12_awlen"   : m12.aw.len,
            "o_m_axi12_awsize"  : m12.aw.size,
            "o_m_axi12_awburst" : m12.aw.burst,
            "o_m_axi12_awvalid" : m12.aw.valid,
            "i_m_axi12_awready" : m12.aw.ready,

            # WRITE DATA
            "o_m_axi12_wdata"   : m12.w.data,
            "o_m_axi12_wstrb"   : m12.w.strb,
            "o_m_axi12_wlast"   : m12.w.last,
            "o_m_axi12_wvalid"  : m12.w.valid,
            "i_m_axi12_wready"  : m12.w.ready,

            # WRITE RESP
            "i_m_axi12_bid"     : m12.b.id,
            "i_m_axi12_bresp"   : m12.b.resp,
            "i_m_axi12_bvalid"  : m12.b.valid,
            "o_m_axi12_bready"  : m12.b.ready,

            # READ ADDRESS
            "o_m_axi12_arid"    : m12.ar.id,
            "o_m_axi12_araddr"  : m12.ar.addr,
            "o_m_axi12_arlen"   : m12.ar.len,
            "o_m_axi12_arsize"  : m12.ar.size,
            "o_m_axi12_arburst" : m12.ar.burst,
            "o_m_axi12_arvalid" : m12.ar.valid,
            "i_m_axi12_arready" : m12.ar.ready,

            # READ DATA
            "i_m_axi12_rid"     : m12.r.id,
            "i_m_axi12_rdata"   : m12.r.data,
            "i_m_axi12_rresp"   : m12.r.resp,
            "i_m_axi12_rlast"   : m12.r.last,
            "i_m_axi12_rvalid"  : m12.r.valid,
            "o_m_axi12_rready"  : m12.r.ready,
        })

        self.cfu_axi13 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m13 = self.cfu_axi13
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi13_awid"    : m13.aw.id,
            "o_m_axi13_awaddr"  : m13.aw.addr,
            "o_m_axi13_awlen"   : m13.aw.len,
            "o_m_axi13_awsize"  : m13.aw.size,
            "o_m_axi13_awburst" : m13.aw.burst,
            "o_m_axi13_awvalid" : m13.aw.valid,
            "i_m_axi13_awready" : m13.aw.ready,

            # WRITE DATA
            "o_m_axi13_wdata"   : m13.w.data,
            "o_m_axi13_wstrb"   : m13.w.strb,
            "o_m_axi13_wlast"   : m13.w.last,
            "o_m_axi13_wvalid"  : m13.w.valid,
            "i_m_axi13_wready"  : m13.w.ready,

            # WRITE RESP
            "i_m_axi13_bid"     : m13.b.id,
            "i_m_axi13_bresp"   : m13.b.resp,
            "i_m_axi13_bvalid"  : m13.b.valid,
            "o_m_axi13_bready"  : m13.b.ready,

            # READ ADDRESS
            "o_m_axi13_arid"    : m13.ar.id,
            "o_m_axi13_araddr"  : m13.ar.addr,
            "o_m_axi13_arlen"   : m13.ar.len,
            "o_m_axi13_arsize"  : m13.ar.size,
            "o_m_axi13_arburst" : m13.ar.burst,
            "o_m_axi13_arvalid" : m13.ar.valid,
            "i_m_axi13_arready" : m13.ar.ready,

            # READ DATA
            "i_m_axi13_rid"     : m13.r.id,
            "i_m_axi13_rdata"   : m13.r.data,
            "i_m_axi13_rresp"   : m13.r.resp,
            "i_m_axi13_rlast"   : m13.r.last,
            "i_m_axi13_rvalid"  : m13.r.valid,
            "o_m_axi13_rready"  : m13.r.ready,
        })

        self.cfu_axi14 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m14 = self.cfu_axi14
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi14_awid"    : m14.aw.id,
            "o_m_axi14_awaddr"  : m14.aw.addr,
            "o_m_axi14_awlen"   : m14.aw.len,
            "o_m_axi14_awsize"  : m14.aw.size,
            "o_m_axi14_awburst" : m14.aw.burst,
            "o_m_axi14_awvalid" : m14.aw.valid,
            "i_m_axi14_awready" : m14.aw.ready,

            # WRITE DATA
            "o_m_axi14_wdata"   : m14.w.data,
            "o_m_axi14_wstrb"   : m14.w.strb,
            "o_m_axi14_wlast"   : m14.w.last,
            "o_m_axi14_wvalid"  : m14.w.valid,
            "i_m_axi14_wready"  : m14.w.ready,

            # WRITE RESP
            "i_m_axi14_bid"     : m14.b.id,
            "i_m_axi14_bresp"   : m14.b.resp,
            "i_m_axi14_bvalid"  : m14.b.valid,
            "o_m_axi14_bready"  : m14.b.ready,

            # READ ADDRESS
            "o_m_axi14_arid"    : m14.ar.id,
            "o_m_axi14_araddr"  : m14.ar.addr,
            "o_m_axi14_arlen"   : m14.ar.len,
            "o_m_axi14_arsize"  : m14.ar.size,
            "o_m_axi14_arburst" : m14.ar.burst,
            "o_m_axi14_arvalid" : m14.ar.valid,
            "i_m_axi14_arready" : m14.ar.ready,

            # READ DATA
            "i_m_axi14_rid"     : m14.r.id,
            "i_m_axi14_rdata"   : m14.r.data,
            "i_m_axi14_rresp"   : m14.r.resp,
            "i_m_axi14_rlast"   : m14.r.last,
            "i_m_axi14_rvalid"  : m14.r.valid,
            "o_m_axi14_rready"  : m14.r.ready,
        })

        self.cfu_axi15 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m15 = self.cfu_axi15
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi15_awid"    : m15.aw.id,
            "o_m_axi15_awaddr"  : m15.aw.addr,
            "o_m_axi15_awlen"   : m15.aw.len,
            "o_m_axi15_awsize"  : m15.aw.size,
            "o_m_axi15_awburst" : m15.aw.burst,
            "o_m_axi15_awvalid" : m15.aw.valid,
            "i_m_axi15_awready" : m15.aw.ready,

            # WRITE DATA
            "o_m_axi15_wdata"   : m15.w.data,
            "o_m_axi15_wstrb"   : m15.w.strb,
            "o_m_axi15_wlast"   : m15.w.last,
            "o_m_axi15_wvalid"  : m15.w.valid,
            "i_m_axi15_wready"  : m15.w.ready,

            # WRITE RESP
            "i_m_axi15_bid"     : m15.b.id,
            "i_m_axi15_bresp"   : m15.b.resp,
            "i_m_axi15_bvalid"  : m15.b.valid,
            "o_m_axi15_bready"  : m15.b.ready,

            # READ ADDRESS
            "o_m_axi15_arid"    : m15.ar.id,
            "o_m_axi15_araddr"  : m15.ar.addr,
            "o_m_axi15_arlen"   : m15.ar.len,
            "o_m_axi15_arsize"  : m15.ar.size,
            "o_m_axi15_arburst" : m15.ar.burst,
            "o_m_axi15_arvalid" : m15.ar.valid,
            "i_m_axi15_arready" : m15.ar.ready,

            # READ DATA
            "i_m_axi15_rid"     : m15.r.id,
            "i_m_axi15_rdata"   : m15.r.data,
            "i_m_axi15_rresp"   : m15.r.resp,
            "i_m_axi15_rlast"   : m15.r.last,
            "i_m_axi15_rvalid"  : m15.r.valid,
            "o_m_axi15_rready"  : m15.r.ready,
        })

        self.cfu_axi16 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m16 = self.cfu_axi16
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi16_awid"    : m16.aw.id,
            "o_m_axi16_awaddr"  : m16.aw.addr,
            "o_m_axi16_awlen"   : m16.aw.len,
            "o_m_axi16_awsize"  : m16.aw.size,
            "o_m_axi16_awburst" : m16.aw.burst,
            "o_m_axi16_awvalid" : m16.aw.valid,
            "i_m_axi16_awready" : m16.aw.ready,

            # WRITE DATA
            "o_m_axi16_wdata"   : m16.w.data,
            "o_m_axi16_wstrb"   : m16.w.strb,
            "o_m_axi16_wlast"   : m16.w.last,
            "o_m_axi16_wvalid"  : m16.w.valid,
            "i_m_axi16_wready"  : m16.w.ready,

            # WRITE RESP
            "i_m_axi16_bid"     : m16.b.id,
            "i_m_axi16_bresp"   : m16.b.resp,
            "i_m_axi16_bvalid"  : m16.b.valid,
            "o_m_axi16_bready"  : m16.b.ready,

            # READ ADDRESS
            "o_m_axi16_arid"    : m16.ar.id,
            "o_m_axi16_araddr"  : m16.ar.addr,
            "o_m_axi16_arlen"   : m16.ar.len,
            "o_m_axi16_arsize"  : m16.ar.size,
            "o_m_axi16_arburst" : m16.ar.burst,
            "o_m_axi16_arvalid" : m16.ar.valid,
            "i_m_axi16_arready" : m16.ar.ready,

            # READ DATA
            "i_m_axi16_rid"     : m16.r.id,
            "i_m_axi16_rdata"   : m16.r.data,
            "i_m_axi16_rresp"   : m16.r.resp,
            "i_m_axi16_rlast"   : m16.r.last,
            "i_m_axi16_rvalid"  : m16.r.valid,
            "o_m_axi16_rready"  : m16.r.ready,
        })

        self.cfu_axi17 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m17 = self.cfu_axi17
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi17_awid"    : m17.aw.id,
            "o_m_axi17_awaddr"  : m17.aw.addr,
            "o_m_axi17_awlen"   : m17.aw.len,
            "o_m_axi17_awsize"  : m17.aw.size,
            "o_m_axi17_awburst" : m17.aw.burst,
            "o_m_axi17_awvalid" : m17.aw.valid,
            "i_m_axi17_awready" : m17.aw.ready,

            # WRITE DATA
            "o_m_axi17_wdata"   : m17.w.data,
            "o_m_axi17_wstrb"   : m17.w.strb,
            "o_m_axi17_wlast"   : m17.w.last,
            "o_m_axi17_wvalid"  : m17.w.valid,
            "i_m_axi17_wready"  : m17.w.ready,

            # WRITE RESP
            "i_m_axi17_bid"     : m17.b.id,
            "i_m_axi17_bresp"   : m17.b.resp,
            "i_m_axi17_bvalid"  : m17.b.valid,
            "o_m_axi17_bready"  : m17.b.ready,

            # READ ADDRESS
            "o_m_axi17_arid"    : m17.ar.id,
            "o_m_axi17_araddr"  : m17.ar.addr,
            "o_m_axi17_arlen"   : m17.ar.len,
            "o_m_axi17_arsize"  : m17.ar.size,
            "o_m_axi17_arburst" : m17.ar.burst,
            "o_m_axi17_arvalid" : m17.ar.valid,
            "i_m_axi17_arready" : m17.ar.ready,

            # READ DATA
            "i_m_axi17_rid"     : m17.r.id,
            "i_m_axi17_rdata"   : m17.r.data,
            "i_m_axi17_rresp"   : m17.r.resp,
            "i_m_axi17_rlast"   : m17.r.last,
            "i_m_axi17_rvalid"  : m17.r.valid,
            "o_m_axi17_rready"  : m17.r.ready,
        })

        self.cfu_axi18 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m18 = self.cfu_axi18
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi18_awid"    : m18.aw.id,
            "o_m_axi18_awaddr"  : m18.aw.addr,
            "o_m_axi18_awlen"   : m18.aw.len,
            "o_m_axi18_awsize"  : m18.aw.size,
            "o_m_axi18_awburst" : m18.aw.burst,
            "o_m_axi18_awvalid" : m18.aw.valid,
            "i_m_axi18_awready" : m18.aw.ready,

            # WRITE DATA
            "o_m_axi18_wdata"   : m18.w.data,
            "o_m_axi18_wstrb"   : m18.w.strb,
            "o_m_axi18_wlast"   : m18.w.last,
            "o_m_axi18_wvalid"  : m18.w.valid,
            "i_m_axi18_wready"  : m18.w.ready,

            # WRITE RESP
            "i_m_axi18_bid"     : m18.b.id,
            "i_m_axi18_bresp"   : m18.b.resp,
            "i_m_axi18_bvalid"  : m18.b.valid,
            "o_m_axi18_bready"  : m18.b.ready,

            # READ ADDRESS
            "o_m_axi18_arid"    : m18.ar.id,
            "o_m_axi18_araddr"  : m18.ar.addr,
            "o_m_axi18_arlen"   : m18.ar.len,
            "o_m_axi18_arsize"  : m18.ar.size,
            "o_m_axi18_arburst" : m18.ar.burst,
            "o_m_axi18_arvalid" : m18.ar.valid,
            "i_m_axi18_arready" : m18.ar.ready,

            # READ DATA
            "i_m_axi18_rid"     : m18.r.id,
            "i_m_axi18_rdata"   : m18.r.data,
            "i_m_axi18_rresp"   : m18.r.resp,
            "i_m_axi18_rlast"   : m18.r.last,
            "i_m_axi18_rvalid"  : m18.r.valid,
            "o_m_axi18_rready"  : m18.r.ready,
        })

        self.cfu_axi19 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m19 = self.cfu_axi19
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi19_awid"    : m19.aw.id,
            "o_m_axi19_awaddr"  : m19.aw.addr,
            "o_m_axi19_awlen"   : m19.aw.len,
            "o_m_axi19_awsize"  : m19.aw.size,
            "o_m_axi19_awburst" : m19.aw.burst,
            "o_m_axi19_awvalid" : m19.aw.valid,
            "i_m_axi19_awready" : m19.aw.ready,

            # WRITE DATA
            "o_m_axi19_wdata"   : m19.w.data,
            "o_m_axi19_wstrb"   : m19.w.strb,
            "o_m_axi19_wlast"   : m19.w.last,
            "o_m_axi19_wvalid"  : m19.w.valid,
            "i_m_axi19_wready"  : m19.w.ready,

            # WRITE RESP
            "i_m_axi19_bid"     : m19.b.id,
            "i_m_axi19_bresp"   : m19.b.resp,
            "i_m_axi19_bvalid"  : m19.b.valid,
            "o_m_axi19_bready"  : m19.b.ready,

            # READ ADDRESS
            "o_m_axi19_arid"    : m19.ar.id,
            "o_m_axi19_araddr"  : m19.ar.addr,
            "o_m_axi19_arlen"   : m19.ar.len,
            "o_m_axi19_arsize"  : m19.ar.size,
            "o_m_axi19_arburst" : m19.ar.burst,
            "o_m_axi19_arvalid" : m19.ar.valid,
            "i_m_axi19_arready" : m19.ar.ready,

            # READ DATA
            "i_m_axi19_rid"     : m19.r.id,
            "i_m_axi19_rdata"   : m19.r.data,
            "i_m_axi19_rresp"   : m19.r.resp,
            "i_m_axi19_rlast"   : m19.r.last,
            "i_m_axi19_rvalid"  : m19.r.valid,
            "o_m_axi19_rready"  : m19.r.ready,
        })

        self.cfu_axi20 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m20 = self.cfu_axi20
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi20_awid"    : m20.aw.id,
            "o_m_axi20_awaddr"  : m20.aw.addr,
            "o_m_axi20_awlen"   : m20.aw.len,
            "o_m_axi20_awsize"  : m20.aw.size,
            "o_m_axi20_awburst" : m20.aw.burst,
            "o_m_axi20_awvalid" : m20.aw.valid,
            "i_m_axi20_awready" : m20.aw.ready,

            # WRITE DATA
            "o_m_axi20_wdata"   : m20.w.data,
            "o_m_axi20_wstrb"   : m20.w.strb,
            "o_m_axi20_wlast"   : m20.w.last,
            "o_m_axi20_wvalid"  : m20.w.valid,
            "i_m_axi20_wready"  : m20.w.ready,

            # WRITE RESP
            "i_m_axi20_bid"     : m20.b.id,
            "i_m_axi20_bresp"   : m20.b.resp,
            "i_m_axi20_bvalid"  : m20.b.valid,
            "o_m_axi20_bready"  : m20.b.ready,

            # READ ADDRESS
            "o_m_axi20_arid"    : m20.ar.id,
            "o_m_axi20_araddr"  : m20.ar.addr,
            "o_m_axi20_arlen"   : m20.ar.len,
            "o_m_axi20_arsize"  : m20.ar.size,
            "o_m_axi20_arburst" : m20.ar.burst,
            "o_m_axi20_arvalid" : m20.ar.valid,
            "i_m_axi20_arready" : m20.ar.ready,

            # READ DATA
            "i_m_axi20_rid"     : m20.r.id,
            "i_m_axi20_rdata"   : m20.r.data,
            "i_m_axi20_rresp"   : m20.r.resp,
            "i_m_axi20_rlast"   : m20.r.last,
            "i_m_axi20_rvalid"  : m20.r.valid,
            "o_m_axi20_rready"  : m20.r.ready,
        })

        self.cfu_axi21 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m21 = self.cfu_axi21
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi21_awid"    : m21.aw.id,
            "o_m_axi21_awaddr"  : m21.aw.addr,
            "o_m_axi21_awlen"   : m21.aw.len,
            "o_m_axi21_awsize"  : m21.aw.size,
            "o_m_axi21_awburst" : m21.aw.burst,
            "o_m_axi21_awvalid" : m21.aw.valid,
            "i_m_axi21_awready" : m21.aw.ready,

            # WRITE DATA
            "o_m_axi21_wdata"   : m21.w.data,
            "o_m_axi21_wstrb"   : m21.w.strb,
            "o_m_axi21_wlast"   : m21.w.last,
            "o_m_axi21_wvalid"  : m21.w.valid,
            "i_m_axi21_wready"  : m21.w.ready,

            # WRITE RESP
            "i_m_axi21_bid"     : m21.b.id,
            "i_m_axi21_bresp"   : m21.b.resp,
            "i_m_axi21_bvalid"  : m21.b.valid,
            "o_m_axi21_bready"  : m21.b.ready,

            # READ ADDRESS
            "o_m_axi21_arid"    : m21.ar.id,
            "o_m_axi21_araddr"  : m21.ar.addr,
            "o_m_axi21_arlen"   : m21.ar.len,
            "o_m_axi21_arsize"  : m21.ar.size,
            "o_m_axi21_arburst" : m21.ar.burst,
            "o_m_axi21_arvalid" : m21.ar.valid,
            "i_m_axi21_arready" : m21.ar.ready,

            # READ DATA
            "i_m_axi21_rid"     : m21.r.id,
            "i_m_axi21_rdata"   : m21.r.data,
            "i_m_axi21_rresp"   : m21.r.resp,
            "i_m_axi21_rlast"   : m21.r.last,
            "i_m_axi21_rvalid"  : m21.r.valid,
            "o_m_axi21_rready"  : m21.r.ready,
        })

        self.cfu_axi22 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m22 = self.cfu_axi22
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi22_awid"    : m22.aw.id,
            "o_m_axi22_awaddr"  : m22.aw.addr,
            "o_m_axi22_awlen"   : m22.aw.len,
            "o_m_axi22_awsize"  : m22.aw.size,
            "o_m_axi22_awburst" : m22.aw.burst,
            "o_m_axi22_awvalid" : m22.aw.valid,
            "i_m_axi22_awready" : m22.aw.ready,

            # WRITE DATA
            "o_m_axi22_wdata"   : m22.w.data,
            "o_m_axi22_wstrb"   : m22.w.strb,
            "o_m_axi22_wlast"   : m22.w.last,
            "o_m_axi22_wvalid"  : m22.w.valid,
            "i_m_axi22_wready"  : m22.w.ready,

            # WRITE RESP
            "i_m_axi22_bid"     : m22.b.id,
            "i_m_axi22_bresp"   : m22.b.resp,
            "i_m_axi22_bvalid"  : m22.b.valid,
            "o_m_axi22_bready"  : m22.b.ready,

            # READ ADDRESS
            "o_m_axi22_arid"    : m22.ar.id,
            "o_m_axi22_araddr"  : m22.ar.addr,
            "o_m_axi22_arlen"   : m22.ar.len,
            "o_m_axi22_arsize"  : m22.ar.size,
            "o_m_axi22_arburst" : m22.ar.burst,
            "o_m_axi22_arvalid" : m22.ar.valid,
            "i_m_axi22_arready" : m22.ar.ready,

            # READ DATA
            "i_m_axi22_rid"     : m22.r.id,
            "i_m_axi22_rdata"   : m22.r.data,
            "i_m_axi22_rresp"   : m22.r.resp,
            "i_m_axi22_rlast"   : m22.r.last,
            "i_m_axi22_rvalid"  : m22.r.valid,
            "o_m_axi22_rready"  : m22.r.ready,
        })

        self.cfu_axi23 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m23 = self.cfu_axi23
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi23_awid"    : m23.aw.id,
            "o_m_axi23_awaddr"  : m23.aw.addr,
            "o_m_axi23_awlen"   : m23.aw.len,
            "o_m_axi23_awsize"  : m23.aw.size,
            "o_m_axi23_awburst" : m23.aw.burst,
            "o_m_axi23_awvalid" : m23.aw.valid,
            "i_m_axi23_awready" : m23.aw.ready,

            # WRITE DATA
            "o_m_axi23_wdata"   : m23.w.data,
            "o_m_axi23_wstrb"   : m23.w.strb,
            "o_m_axi23_wlast"   : m23.w.last,
            "o_m_axi23_wvalid"  : m23.w.valid,
            "i_m_axi23_wready"  : m23.w.ready,

            # WRITE RESP
            "i_m_axi23_bid"     : m23.b.id,
            "i_m_axi23_bresp"   : m23.b.resp,
            "i_m_axi23_bvalid"  : m23.b.valid,
            "o_m_axi23_bready"  : m23.b.ready,

            # READ ADDRESS
            "o_m_axi23_arid"    : m23.ar.id,
            "o_m_axi23_araddr"  : m23.ar.addr,
            "o_m_axi23_arlen"   : m23.ar.len,
            "o_m_axi23_arsize"  : m23.ar.size,
            "o_m_axi23_arburst" : m23.ar.burst,
            "o_m_axi23_arvalid" : m23.ar.valid,
            "i_m_axi23_arready" : m23.ar.ready,

            # READ DATA
            "i_m_axi23_rid"     : m23.r.id,
            "i_m_axi23_rdata"   : m23.r.data,
            "i_m_axi23_rresp"   : m23.r.resp,
            "i_m_axi23_rlast"   : m23.r.last,
            "i_m_axi23_rvalid"  : m23.r.valid,
            "o_m_axi23_rready"  : m23.r.ready,
        })

        self.cfu_axi24 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m24 = self.cfu_axi24
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi24_awid"    : m24.aw.id,
            "o_m_axi24_awaddr"  : m24.aw.addr,
            "o_m_axi24_awlen"   : m24.aw.len,
            "o_m_axi24_awsize"  : m24.aw.size,
            "o_m_axi24_awburst" : m24.aw.burst,
            "o_m_axi24_awvalid" : m24.aw.valid,
            "i_m_axi24_awready" : m24.aw.ready,

            # WRITE DATA
            "o_m_axi24_wdata"   : m24.w.data,
            "o_m_axi24_wstrb"   : m24.w.strb,
            "o_m_axi24_wlast"   : m24.w.last,
            "o_m_axi24_wvalid"  : m24.w.valid,
            "i_m_axi24_wready"  : m24.w.ready,

            # WRITE RESP
            "i_m_axi24_bid"     : m24.b.id,
            "i_m_axi24_bresp"   : m24.b.resp,
            "i_m_axi24_bvalid"  : m24.b.valid,
            "o_m_axi24_bready"  : m24.b.ready,

            # READ ADDRESS
            "o_m_axi24_arid"    : m24.ar.id,
            "o_m_axi24_araddr"  : m24.ar.addr,
            "o_m_axi24_arlen"   : m24.ar.len,
            "o_m_axi24_arsize"  : m24.ar.size,
            "o_m_axi24_arburst" : m24.ar.burst,
            "o_m_axi24_arvalid" : m24.ar.valid,
            "i_m_axi24_arready" : m24.ar.ready,

            # READ DATA
            "i_m_axi24_rid"     : m24.r.id,
            "i_m_axi24_rdata"   : m24.r.data,
            "i_m_axi24_rresp"   : m24.r.resp,
            "i_m_axi24_rlast"   : m24.r.last,
            "i_m_axi24_rvalid"  : m24.r.valid,
            "o_m_axi24_rready"  : m24.r.ready,
        })

        self.cfu_axi25 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m25 = self.cfu_axi25
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi25_awid"    : m25.aw.id,
            "o_m_axi25_awaddr"  : m25.aw.addr,
            "o_m_axi25_awlen"   : m25.aw.len,
            "o_m_axi25_awsize"  : m25.aw.size,
            "o_m_axi25_awburst" : m25.aw.burst,
            "o_m_axi25_awvalid" : m25.aw.valid,
            "i_m_axi25_awready" : m25.aw.ready,

            # WRITE DATA
            "o_m_axi25_wdata"   : m25.w.data,
            "o_m_axi25_wstrb"   : m25.w.strb,
            "o_m_axi25_wlast"   : m25.w.last,
            "o_m_axi25_wvalid"  : m25.w.valid,
            "i_m_axi25_wready"  : m25.w.ready,

            # WRITE RESP
            "i_m_axi25_bid"     : m25.b.id,
            "i_m_axi25_bresp"   : m25.b.resp,
            "i_m_axi25_bvalid"  : m25.b.valid,
            "o_m_axi25_bready"  : m25.b.ready,

            # READ ADDRESS
            "o_m_axi25_arid"    : m25.ar.id,
            "o_m_axi25_araddr"  : m25.ar.addr,
            "o_m_axi25_arlen"   : m25.ar.len,
            "o_m_axi25_arsize"  : m25.ar.size,
            "o_m_axi25_arburst" : m25.ar.burst,
            "o_m_axi25_arvalid" : m25.ar.valid,
            "i_m_axi25_arready" : m25.ar.ready,

            # READ DATA
            "i_m_axi25_rid"     : m25.r.id,
            "i_m_axi25_rdata"   : m25.r.data,
            "i_m_axi25_rresp"   : m25.r.resp,
            "i_m_axi25_rlast"   : m25.r.last,
            "i_m_axi25_rvalid"  : m25.r.valid,
            "o_m_axi25_rready"  : m25.r.ready,
        })

        self.cfu_axi26 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m26 = self.cfu_axi26
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi26_awid"    : m26.aw.id,
            "o_m_axi26_awaddr"  : m26.aw.addr,
            "o_m_axi26_awlen"   : m26.aw.len,
            "o_m_axi26_awsize"  : m26.aw.size,
            "o_m_axi26_awburst" : m26.aw.burst,
            "o_m_axi26_awvalid" : m26.aw.valid,
            "i_m_axi26_awready" : m26.aw.ready,

            # WRITE DATA
            "o_m_axi26_wdata"   : m26.w.data,
            "o_m_axi26_wstrb"   : m26.w.strb,
            "o_m_axi26_wlast"   : m26.w.last,
            "o_m_axi26_wvalid"  : m26.w.valid,
            "i_m_axi26_wready"  : m26.w.ready,

            # WRITE RESP
            "i_m_axi26_bid"     : m26.b.id,
            "i_m_axi26_bresp"   : m26.b.resp,
            "i_m_axi26_bvalid"  : m26.b.valid,
            "o_m_axi26_bready"  : m26.b.ready,

            # READ ADDRESS
            "o_m_axi26_arid"    : m26.ar.id,
            "o_m_axi26_araddr"  : m26.ar.addr,
            "o_m_axi26_arlen"   : m26.ar.len,
            "o_m_axi26_arsize"  : m26.ar.size,
            "o_m_axi26_arburst" : m26.ar.burst,
            "o_m_axi26_arvalid" : m26.ar.valid,
            "i_m_axi26_arready" : m26.ar.ready,

            # READ DATA
            "i_m_axi26_rid"     : m26.r.id,
            "i_m_axi26_rdata"   : m26.r.data,
            "i_m_axi26_rresp"   : m26.r.resp,
            "i_m_axi26_rlast"   : m26.r.last,
            "i_m_axi26_rvalid"  : m26.r.valid,
            "o_m_axi26_rready"  : m26.r.ready,
        })

        self.cfu_axi27 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m27 = self.cfu_axi27
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi27_awid"    : m27.aw.id,
            "o_m_axi27_awaddr"  : m27.aw.addr,
            "o_m_axi27_awlen"   : m27.aw.len,
            "o_m_axi27_awsize"  : m27.aw.size,
            "o_m_axi27_awburst" : m27.aw.burst,
            "o_m_axi27_awvalid" : m27.aw.valid,
            "i_m_axi27_awready" : m27.aw.ready,

            # WRITE DATA
            "o_m_axi27_wdata"   : m27.w.data,
            "o_m_axi27_wstrb"   : m27.w.strb,
            "o_m_axi27_wlast"   : m27.w.last,
            "o_m_axi27_wvalid"  : m27.w.valid,
            "i_m_axi27_wready"  : m27.w.ready,

            # WRITE RESP
            "i_m_axi27_bid"     : m27.b.id,
            "i_m_axi27_bresp"   : m27.b.resp,
            "i_m_axi27_bvalid"  : m27.b.valid,
            "o_m_axi27_bready"  : m27.b.ready,

            # READ ADDRESS
            "o_m_axi27_arid"    : m27.ar.id,
            "o_m_axi27_araddr"  : m27.ar.addr,
            "o_m_axi27_arlen"   : m27.ar.len,
            "o_m_axi27_arsize"  : m27.ar.size,
            "o_m_axi27_arburst" : m27.ar.burst,
            "o_m_axi27_arvalid" : m27.ar.valid,
            "i_m_axi27_arready" : m27.ar.ready,

            # READ DATA
            "i_m_axi27_rid"     : m27.r.id,
            "i_m_axi27_rdata"   : m27.r.data,
            "i_m_axi27_rresp"   : m27.r.resp,
            "i_m_axi27_rlast"   : m27.r.last,
            "i_m_axi27_rvalid"  : m27.r.valid,
            "o_m_axi27_rready"  : m27.r.ready,
        })

        self.cfu_axi28 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m28 = self.cfu_axi28
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi28_awid"    : m28.aw.id,
            "o_m_axi28_awaddr"  : m28.aw.addr,
            "o_m_axi28_awlen"   : m28.aw.len,
            "o_m_axi28_awsize"  : m28.aw.size,
            "o_m_axi28_awburst" : m28.aw.burst,
            "o_m_axi28_awvalid" : m28.aw.valid,
            "i_m_axi28_awready" : m28.aw.ready,

            # WRITE DATA
            "o_m_axi28_wdata"   : m28.w.data,
            "o_m_axi28_wstrb"   : m28.w.strb,
            "o_m_axi28_wlast"   : m28.w.last,
            "o_m_axi28_wvalid"  : m28.w.valid,
            "i_m_axi28_wready"  : m28.w.ready,

            # WRITE RESP
            "i_m_axi28_bid"     : m28.b.id,
            "i_m_axi28_bresp"   : m28.b.resp,
            "i_m_axi28_bvalid"  : m28.b.valid,
            "o_m_axi28_bready"  : m28.b.ready,

            # READ ADDRESS
            "o_m_axi28_arid"    : m28.ar.id,
            "o_m_axi28_araddr"  : m28.ar.addr,
            "o_m_axi28_arlen"   : m28.ar.len,
            "o_m_axi28_arsize"  : m28.ar.size,
            "o_m_axi28_arburst" : m28.ar.burst,
            "o_m_axi28_arvalid" : m28.ar.valid,
            "i_m_axi28_arready" : m28.ar.ready,

            # READ DATA
            "i_m_axi28_rid"     : m28.r.id,
            "i_m_axi28_rdata"   : m28.r.data,
            "i_m_axi28_rresp"   : m28.r.resp,
            "i_m_axi28_rlast"   : m28.r.last,
            "i_m_axi28_rvalid"  : m28.r.valid,
            "o_m_axi28_rready"  : m28.r.ready,
        })

        self.cfu_axi29 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m29 = self.cfu_axi29
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi29_awid"    : m29.aw.id,
            "o_m_axi29_awaddr"  : m29.aw.addr,
            "o_m_axi29_awlen"   : m29.aw.len,
            "o_m_axi29_awsize"  : m29.aw.size,
            "o_m_axi29_awburst" : m29.aw.burst,
            "o_m_axi29_awvalid" : m29.aw.valid,
            "i_m_axi29_awready" : m29.aw.ready,

            # WRITE DATA
            "o_m_axi29_wdata"   : m29.w.data,
            "o_m_axi29_wstrb"   : m29.w.strb,
            "o_m_axi29_wlast"   : m29.w.last,
            "o_m_axi29_wvalid"  : m29.w.valid,
            "i_m_axi29_wready"  : m29.w.ready,

            # WRITE RESP
            "i_m_axi29_bid"     : m29.b.id,
            "i_m_axi29_bresp"   : m29.b.resp,
            "i_m_axi29_bvalid"  : m29.b.valid,
            "o_m_axi29_bready"  : m29.b.ready,

            # READ ADDRESS
            "o_m_axi29_arid"    : m29.ar.id,
            "o_m_axi29_araddr"  : m29.ar.addr,
            "o_m_axi29_arlen"   : m29.ar.len,
            "o_m_axi29_arsize"  : m29.ar.size,
            "o_m_axi29_arburst" : m29.ar.burst,
            "o_m_axi29_arvalid" : m29.ar.valid,
            "i_m_axi29_arready" : m29.ar.ready,

            # READ DATA
            "i_m_axi29_rid"     : m29.r.id,
            "i_m_axi29_rdata"   : m29.r.data,
            "i_m_axi29_rresp"   : m29.r.resp,
            "i_m_axi29_rlast"   : m29.r.last,
            "i_m_axi29_rvalid"  : m29.r.valid,
            "o_m_axi29_rready"  : m29.r.ready,
        })

        self.cfu_axi30 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m30 = self.cfu_axi30
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi30_awid"    : m30.aw.id,
            "o_m_axi30_awaddr"  : m30.aw.addr,
            "o_m_axi30_awlen"   : m30.aw.len,
            "o_m_axi30_awsize"  : m30.aw.size,
            "o_m_axi30_awburst" : m30.aw.burst,
            "o_m_axi30_awvalid" : m30.aw.valid,
            "i_m_axi30_awready" : m30.aw.ready,

            # WRITE DATA
            "o_m_axi30_wdata"   : m30.w.data,
            "o_m_axi30_wstrb"   : m30.w.strb,
            "o_m_axi30_wlast"   : m30.w.last,
            "o_m_axi30_wvalid"  : m30.w.valid,
            "i_m_axi30_wready"  : m30.w.ready,

            # WRITE RESP
            "i_m_axi30_bid"     : m30.b.id,
            "i_m_axi30_bresp"   : m30.b.resp,
            "i_m_axi30_bvalid"  : m30.b.valid,
            "o_m_axi30_bready"  : m30.b.ready,

            # READ ADDRESS
            "o_m_axi30_arid"    : m30.ar.id,
            "o_m_axi30_araddr"  : m30.ar.addr,
            "o_m_axi30_arlen"   : m30.ar.len,
            "o_m_axi30_arsize"  : m30.ar.size,
            "o_m_axi30_arburst" : m30.ar.burst,
            "o_m_axi30_arvalid" : m30.ar.valid,
            "i_m_axi30_arready" : m30.ar.ready,

            # READ DATA
            "i_m_axi30_rid"     : m30.r.id,
            "i_m_axi30_rdata"   : m30.r.data,
            "i_m_axi30_rresp"   : m30.r.resp,
            "i_m_axi30_rlast"   : m30.r.last,
            "i_m_axi30_rvalid"  : m30.r.valid,
            "o_m_axi30_rready"  : m30.r.ready,
        })

        self.cfu_axi31 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m31 = self.cfu_axi31
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi31_awid"    : m31.aw.id,
            "o_m_axi31_awaddr"  : m31.aw.addr,
            "o_m_axi31_awlen"   : m31.aw.len,
            "o_m_axi31_awsize"  : m31.aw.size,
            "o_m_axi31_awburst" : m31.aw.burst,
            "o_m_axi31_awvalid" : m31.aw.valid,
            "i_m_axi31_awready" : m31.aw.ready,

            # WRITE DATA
            "o_m_axi31_wdata"   : m31.w.data,
            "o_m_axi31_wstrb"   : m31.w.strb,
            "o_m_axi31_wlast"   : m31.w.last,
            "o_m_axi31_wvalid"  : m31.w.valid,
            "i_m_axi31_wready"  : m31.w.ready,

            # WRITE RESP
            "i_m_axi31_bid"     : m31.b.id,
            "i_m_axi31_bresp"   : m31.b.resp,
            "i_m_axi31_bvalid"  : m31.b.valid,
            "o_m_axi31_bready"  : m31.b.ready,

            # READ ADDRESS
            "o_m_axi31_arid"    : m31.ar.id,
            "o_m_axi31_araddr"  : m31.ar.addr,
            "o_m_axi31_arlen"   : m31.ar.len,
            "o_m_axi31_arsize"  : m31.ar.size,
            "o_m_axi31_arburst" : m31.ar.burst,
            "o_m_axi31_arvalid" : m31.ar.valid,
            "i_m_axi31_arready" : m31.ar.ready,

            # READ DATA
            "i_m_axi31_rid"     : m31.r.id,
            "i_m_axi31_rdata"   : m31.r.data,
            "i_m_axi31_rresp"   : m31.r.resp,
            "i_m_axi31_rlast"   : m31.r.last,
            "i_m_axi31_rvalid"  : m31.r.valid,
            "o_m_axi31_rready"  : m31.r.ready,
        })

        self.cfu_axi32 = AXIInterface(data_width=256, address_width=33, id_width=4)
        m32 = self.cfu_axi32
        self.cfu_params.update({
            # WRITE ADDRESS
            "o_m_axi32_awid"    : m32.aw.id,
            "o_m_axi32_awaddr"  : m32.aw.addr,
            "o_m_axi32_awlen"   : m32.aw.len,
            "o_m_axi32_awsize"  : m32.aw.size,
            "o_m_axi32_awburst" : m32.aw.burst,
            "o_m_axi32_awvalid" : m32.aw.valid,
            "i_m_axi32_awready" : m32.aw.ready,

            # WRITE DATA
            "o_m_axi32_wdata"   : m32.w.data,
            "o_m_axi32_wstrb"   : m32.w.strb,
            "o_m_axi32_wlast"   : m32.w.last,
            "o_m_axi32_wvalid"  : m32.w.valid,
            "i_m_axi32_wready"  : m32.w.ready,

            # WRITE RESP
            "i_m_axi32_bid"     : m32.b.id,
            "i_m_axi32_bresp"   : m32.b.resp,
            "i_m_axi32_bvalid"  : m32.b.valid,
            "o_m_axi32_bready"  : m32.b.ready,

            # READ ADDRESS
            "o_m_axi32_arid"    : m32.ar.id,
            "o_m_axi32_araddr"  : m32.ar.addr,
            "o_m_axi32_arlen"   : m32.ar.len,
            "o_m_axi32_arsize"  : m32.ar.size,
            "o_m_axi32_arburst" : m32.ar.burst,
            "o_m_axi32_arvalid" : m32.ar.valid,
            "i_m_axi32_arready" : m32.ar.ready,

            # READ DATA
            "i_m_axi32_rid"     : m32.r.id,
            "i_m_axi32_rdata"   : m32.r.data,
            "i_m_axi32_rresp"   : m32.r.resp,
            "i_m_axi32_rlast"   : m32.r.last,
            "i_m_axi32_rvalid"  : m32.r.valid,
            "o_m_axi32_rready"  : m32.r.ready,
        })

    @staticmethod
    def add_sources(platform, variant="standard"):
        cpu_filename = CPU_VARIANTS[variant] + ".v"
        vdir = get_data_mod("cpu", "vexriscv").data_location
        platform.add_source(os.path.join(vdir, cpu_filename))

    def add_soc_components(self, soc, soc_region_cls):
        # Connect Debug interface to SoC.
        if "debug" in self.variant:
            soc.bus.add_slave("vexriscv_debug", self.debug_bus, region=
                soc_region_cls(
                    origin = soc.mem_map.get("vexriscv_debug"),
                    size   = 0x100,
                    cached = False
                )
            )

        # Pass I/D Caches info to software.
        base_variant = str(self.variant.split('+')[0])
        # DCACHE is present on all variants except minimal and lite.
        if not base_variant in ["minimal", "lite"]:
            soc.add_config("CPU_HAS_DCACHE")
        # ICACHE is present on all variants except minimal.
        if not base_variant in ["minimal"]:
            soc.add_config("CPU_HAS_ICACHE")

    def use_external_variant(self, variant_filename):
        self.external_variant = True
        self.platform.add_source(variant_filename)

    def do_finalize(self):
        assert hasattr(self, "reset_address")
        if not self.external_variant:
            self.add_sources(self.platform, self.variant)
        self.specials += Instance("VexRiscv", **self.cpu_params)
        if hasattr(self, "cfu_params"):
            self.specials += Instance("Cfu", **self.cfu_params)
