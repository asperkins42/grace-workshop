#!/usr/bin/env python3
#"""Alveo U280 target with a CFU-to-HBM AXI bridge."""

import os

from migen import *
from migen.genlib.cdc import ClockDomainsRenamer

from litex_boards.platforms import xilinx_alveo_u280

from litex.gen.common import log2_int
from litex.soc.cores.clock import USIDELAYCTRL, USMMCM
from litex.soc.cores.ram.xilinx_usp_hbm2 import USPHBM2
from litex.soc.integration.builder import Builder, builder_argdict, builder_args
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.soc_core import SoCCore, soc_core_argdict, soc_core_args
from litex.soc.interconnect import wishbone
from litex.soc.interconnect.axi import (
    RESP_OKAY,
    AXICrossbar,
    AXIInterface,
    AXILite2AXI,
    AXILiteInterface,
    AXIUpConverter,
    Wishbone2AXILite,
)

from litedram.modules import MTA18ASF2G72PZ
from litedram.phy import usddrphy

class AXILite32toAXI256Bridge(Module):
    """Bridge a 32-bit AXI-Lite master to a 256-bit AXI master with read-modify-write support."""

    def __init__(self, axi_lite, axi):
        assert isinstance(axi_lite, AXILiteInterface)
        assert isinstance(axi, AXIInterface)
        assert axi_lite.data_width == 32
        assert axi.data_width == 256
        assert axi_lite.address_width == axi.address_width

        burst_size = log2_int(axi.data_width // 8)  # 5 for 256-bit
        burst_type_incr = 0b01

        # ---------------------------------------------------------------------
        # Write path buffering / state
        # ---------------------------------------------------------------------
        aw_buf_valid = Signal()
        aw_addr_buf = Signal(axi.address_width)
        aw_lane_buf = Signal(3)

        w_buf_valid = Signal()
        w_data_buf = Signal(32)
        w_strb_buf = Signal(4)

        ar_buf_valid = Signal()
        ar_addr_buf = Signal(axi.address_width)
        ar_lane_buf = Signal(3)
        read_inflight = Signal()
        read_data_valid = Signal()
        read_data_buf = Signal(32)
        read_resp_buf = Signal(2)

        write_state = Signal(3)
        WRITE_IDLE, WRITE_READ_REQ, WRITE_READ_DATA, WRITE_PREP, WRITE_SEND, WRITE_WAIT_B, WRITE_RESP = range(7)

        read_line_buf = Signal(axi.data_width)
        write_line_buf = Signal(axi.data_width)
        b_resp_buf = Signal(2)
        b_buf_valid = Signal()

        write_aw_done = Signal()
        write_w_done = Signal()

        write_can_start = Signal()
        self.comb += write_can_start.eq(~ar_buf_valid & ~read_inflight & ~read_data_valid)

        # Expand 32-bit payloads/byte-enables into lane-aligned 256-bit masks.
        masked_w_data = Cat(*[Mux(w_strb_buf[i], w_data_buf[8*i:8*(i+1)], 0) for i in range(4)])
        mask32 = Cat(*[Replicate(w_strb_buf[i], 8) for i in range(4)])
        shift_bits = Cat(C(0, 5), aw_lane_buf)
        line_mask_value = (Cat(mask32, C(0, 224))) << shift_bits
        line_update_value = (Cat(masked_w_data, C(0, 224))) << shift_bits
        updated_line_value = (read_line_buf & ~line_mask_value) | line_update_value

        # AXI-Lite handshake for address/data channels.
        self.comb += [
            axi_lite.aw.ready.eq((write_state == WRITE_IDLE) & ~aw_buf_valid),
            axi_lite.w.ready.eq((write_state == WRITE_IDLE) & ~w_buf_valid),
        ]

        # Capture AXI-Lite write request.
        self.sync += [
            If(axi_lite.aw.valid & axi_lite.aw.ready,
                aw_buf_valid.eq(1),
                aw_addr_buf.eq(Cat(C(0, 5), axi_lite.aw.addr[5:])),
                aw_lane_buf.eq(axi_lite.aw.addr[2:5])
            ),
            If(axi_lite.w.valid & axi_lite.w.ready,
                w_buf_valid.eq(1),
                w_data_buf.eq(axi_lite.w.data),
                w_strb_buf.eq(axi_lite.w.strb)
            ),
            If((write_state == WRITE_RESP) & axi_lite.b.valid & axi_lite.b.ready,
                aw_buf_valid.eq(0),
                w_buf_valid.eq(0)
            )
        ]

        # Write state machine.
        self.sync += [
            If(write_state == WRITE_IDLE,
                If(aw_buf_valid & w_buf_valid & write_can_start,
                    write_aw_done.eq(0),
                    write_w_done.eq(0),
                    b_buf_valid.eq(0),
                    b_resp_buf.eq(RESP_OKAY),
                    If(w_strb_buf == 0,
                        b_buf_valid.eq(1),
                        write_state.eq(WRITE_RESP)
                    ).Else(
                        write_state.eq(WRITE_READ_REQ)
                    )
                )
            ).Elif(write_state == WRITE_READ_REQ,
                If(axi.ar.ready,
                    write_state.eq(WRITE_READ_DATA)
                )
            ).Elif(write_state == WRITE_READ_DATA,
                If(axi.r.valid,
                    read_line_buf.eq(axi.r.data),
                    b_resp_buf.eq(axi.r.resp),
                    If(axi.r.last,
                        If(axi.r.resp == RESP_OKAY,
                            write_state.eq(WRITE_PREP)
                        ).Else(
                            write_aw_done.eq(0),
                            write_w_done.eq(0),
                            b_buf_valid.eq(1),
                            write_state.eq(WRITE_RESP)
                        )
                    )
                )
            ).Elif(write_state == WRITE_PREP,
                write_line_buf.eq(updated_line_value),
                write_aw_done.eq(0),
                write_w_done.eq(0),
                write_state.eq(WRITE_SEND)
            ).Elif(write_state == WRITE_SEND,
                If((~write_aw_done) & axi.aw.valid & axi.aw.ready,
                    write_aw_done.eq(1)
                ),
                If((~write_w_done) & axi.w.valid & axi.w.ready,
                    write_w_done.eq(1)
                ),
                If(write_aw_done & write_w_done,
                    write_state.eq(WRITE_WAIT_B)
                )
            ).Elif(write_state == WRITE_WAIT_B,
                If(axi.b.valid & axi.b.ready,
                    b_resp_buf.eq(axi.b.resp),
                    b_buf_valid.eq(1),
                    write_state.eq(WRITE_RESP)
                )
            ).Elif(write_state == WRITE_RESP,
                If(axi_lite.b.valid & axi_lite.b.ready,
                    b_buf_valid.eq(0),
                    write_state.eq(WRITE_IDLE)
                )
            )
        ]

        # Default AXI write channel assignments.
        self.comb += [
            axi.aw.len.eq(0),
            axi.aw.size.eq(burst_size),
            axi.aw.burst.eq(burst_type_incr),
            axi.aw.lock.eq(0),
            axi.aw.prot.eq(0),
            axi.aw.cache.eq(0b0011),
            axi.aw.qos.eq(0),
            axi.aw.id.eq(0),

            axi.aw.valid.eq((write_state == WRITE_SEND) & ~write_aw_done),
            axi.aw.addr.eq(aw_addr_buf),

            axi.w.valid.eq((write_state == WRITE_SEND) & ~write_w_done),
            axi.w.data.eq(write_line_buf),
            axi.w.strb.eq(C(0xFFFFFFFF, 32)),
            axi.w.last.eq(1),
            axi.w.id.eq(0),

            axi.b.ready.eq(write_state == WRITE_WAIT_B),

            axi_lite.b.valid.eq(b_buf_valid),
            axi_lite.b.resp.eq(b_resp_buf),
        ]

        # ---------------------------------------------------------------------
        # Read channel handling (AXI-Lite reads)
        # ---------------------------------------------------------------------
        self.comb += axi_lite.ar.ready.eq(
            (write_state == WRITE_IDLE) &
            ~aw_buf_valid & ~w_buf_valid &
            ~ar_buf_valid & ~read_inflight & ~read_data_valid
        )

        lane_read_data = Array(axi.r.data[lane*32:(lane+1)*32] for lane in range(8))

        self.sync += [
            If(axi_lite.ar.valid & axi_lite.ar.ready,
                ar_buf_valid.eq(1),
                ar_addr_buf.eq(Cat(C(0, 5), axi_lite.ar.addr[5:])),
                ar_lane_buf.eq(axi_lite.ar.addr[2:5])
            ).Elif((write_state != WRITE_READ_REQ) & axi.ar.valid & axi.ar.ready,
                ar_buf_valid.eq(0),
                read_inflight.eq(1)
            ),
            If((write_state != WRITE_READ_DATA) & axi.r.valid & axi.r.ready,
                read_inflight.eq(0),
                read_data_valid.eq(1),
                read_data_buf.eq(lane_read_data[ar_lane_buf]),
                read_resp_buf.eq(axi.r.resp)
            ),
            If(read_data_valid & axi_lite.r.valid & axi_lite.r.ready,
                read_data_valid.eq(0)
            )
        ]

        # Shared AXI read channel between write RMW and read path.
        self.comb += [
            If(write_state == WRITE_READ_REQ,
                axi.ar.valid.eq(1),
                axi.ar.addr.eq(aw_addr_buf)
            ).Else(
                axi.ar.valid.eq(ar_buf_valid),
                axi.ar.addr.eq(ar_addr_buf)
            ),
            axi.ar.len.eq(0),
            axi.ar.size.eq(burst_size),
            axi.ar.burst.eq(burst_type_incr),
            axi.ar.lock.eq(0),
            axi.ar.prot.eq(0),
            axi.ar.cache.eq(0b0011),
            axi.ar.qos.eq(0),
            axi.ar.id.eq(0),

            If(write_state == WRITE_READ_DATA,
                axi.r.ready.eq(1)
            ).Else(
                axi.r.ready.eq(~read_data_valid | axi_lite.r.ready)
            ),

            axi_lite.r.valid.eq(read_data_valid),
            axi_lite.r.data.eq(read_data_buf),
            axi_lite.r.resp.eq(read_resp_buf)
        ]

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq, ddram_channel, with_hbm):
        if with_hbm:
            self.clock_domains.cd_sys = ClockDomain()
            self.clock_domains.cd_hbm_ref = ClockDomain()
            self.clock_domains.cd_apb = ClockDomain()
        else:
            self.rst = Signal()
            self.clock_domains.cd_sys = ClockDomain()
            self.clock_domains.cd_sys4x = ClockDomain()
            self.clock_domains.cd_pll4x = ClockDomain()
            self.clock_domains.cd_idelay = ClockDomain()
        


        if with_hbm:
            self.submodules.pll = pll = USMMCM(speedgrade=-2)
            pll.register_clkin(platform.request("sysclk", ddram_channel), 100e6)
            pll.create_clkout(self.cd_sys, sys_clk_freq)
            pll.create_clkout(self.cd_hbm_ref, 100e6)
            pll.create_clkout(self.cd_apb, 100e6)
            platform.add_false_path_constraints(self.cd_sys.clk, self.cd_apb.clk)
        else:
            self.submodules.pll = pll = USMMCM(speedgrade=-2)
            self.comb += pll.reset.eq(self.rst)
            pll.register_clkin(platform.request("sysclk", ddram_channel), 100e6)
            pll.create_clkout(self.cd_pll4x, sys_clk_freq*4, buf=None, with_reset=False)
            pll.create_clkout(self.cd_idelay, 600e6)
            self.specials += [
                Instance("BUFGCE_DIV",
                    p_BUFGCE_DIVIDE=4,
                    i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys.clk),
                Instance("BUFGCE",
                    i_CE=1, i_I=self.cd_pll4x.clk, o_O=self.cd_sys4x.clk),
            ]

            self.submodules.idelayctrl = USIDELAYCTRL(cd_ref=self.cd_idelay, cd_sys=self.cd_sys)


class BaseSoC(SoCCore):
    def __init__(
        self,
        sys_clk_freq=int(250e6),
        ddram_channel=0,
        with_hbm=False,
        cfu_hbm_port=0,
        cpu_hbm_ports=(1, 2, 3),
        expose_cfu_port_window=False,
        cfu_port_window_base=0x4800_0000,
        cfu_port_window_size=0x0100_0000,
        **kwargs,
    ):
    
        platform = xilinx_alveo_u280.Platform()
        if with_hbm:
            assert 225e6 <= sys_clk_freq <= 450e6

        # Clock/Reset generation
        self.submodules.crg = _CRG(platform, sys_clk_freq, ddram_channel, with_hbm)

        # Base SoC
        SoCCore.__init__(
            self,
            platform,
            sys_clk_freq,
            ident="LiteX SoC on U280 CFU<->HBM bridge",
            **kwargs,
        )

        cpu_cfu_path = kwargs.get("cpu_cfu", None)
        if cpu_cfu_path:
            incdir = os.path.dirname(cpu_cfu_path)
            self.platform.add_verilog_include_path(incdir)
            print(f"[cfu-hbm] Added Verilog include path: {incdir}")
        else:
            print("[cfu-hbm] WARNING: --cpu-cfu not provided; CFU include path not added")

        if with_hbm:
            cpu_hbm_ports = tuple(sorted({int(p) for p in cpu_hbm_ports}))
            if not cpu_hbm_ports:
                raise ValueError("cpu_hbm_ports must contain at least one port when --with-hbm is set.")
            if cfu_hbm_port in cpu_hbm_ports:
                raise ValueError("cfu_hbm_port must be different from the CPU-exposed HBM ports.")
            max_port = 31
            for port in (cfu_hbm_port,) + cpu_hbm_ports:
                if port < 0 or port > max_port:
                    raise ValueError(f"HBM port {port} is outside the valid 0..{max_port} range.")

            self.submodules.hbm = hbm = ClockDomainsRenamer({"axi": "sys"})(USPHBM2(platform))

            hbm_xci = os.path.join("ip", "hbm", "hbm_0.xci")
            if not os.path.exists(hbm_xci):
                os.makedirs(os.path.dirname(hbm_xci), exist_ok=True)
                tmp_name = "hbm_0.xci.txt"
                if os.system(f"wget https://github.com/litex-hub/litex-boards/files/6893157/{tmp_name}") == 0:
                    os.replace(tmp_name, hbm_xci)
                else:
                    raise RuntimeError("Failed to download hbm_0.xci; please place the file manually.")

            def port_origin(port):
                return 0x4000_0000 + 0x1000_0000 * port

            main_ram_base = port_origin(min(cpu_hbm_ports))
            self.mem_map["main_ram"] = main_ram_base
            self.add_config("MAIN_RAM_BASE", main_ram_base)
            self.bus.add_region(
                "main_ram",
                SoCRegion(origin=main_ram_base, size=0x1000_0000, linker=True),
            )

            for port in cpu_hbm_ports:
                axi_port = hbm.axi[port]
                axi_lite = AXILiteInterface(
                    data_width=axi_port.data_width,
                    address_width=axi_port.address_width,
                )
                self.submodules += AXILite2AXI(axi_lite, axi_port)
                self.bus.add_slave(
                    name=f"hbm{port}",
                    slave=axi_lite,
                    region=SoCRegion(origin=port_origin(port), size=0x1000_0000),
                )

            cfu_axi = self.cpu.cfu_axi
            hbm_slave = hbm.axi[cfu_hbm_port]
            cfu_master = cfu_axi
            if cfu_axi.data_width != hbm_slave.data_width:
                cfu_wide = AXIInterface(
                    data_width=hbm_slave.data_width,
                    address_width=cfu_axi.address_width,
                    id_width=getattr(cfu_axi, "id_width", 1),
                )
                self.submodules.cfu_axi_up = AXIUpConverter(cfu_axi, cfu_wide)
                cfu_master = cfu_wide

            masters = [cfu_master]

            if expose_cfu_port_window:
                wishbone_shift = log2_int(self.bus.data_width // 8)
                debug_wb = wishbone.Interface(
                    data_width=self.bus.data_width,
                    adr_width=hbm_slave.address_width - wishbone_shift,
                )
                self.bus.add_slave(
                    name=f"hbm{cfu_hbm_port}_dbg",
                    slave=debug_wb,
                    region=SoCRegion(
                        origin=cfu_port_window_base,
                        size=cfu_port_window_size,
                        cached=True,
                    ),
                )

                debug_axil32 = AXILiteInterface(
                    data_width=32,
                    address_width=hbm_slave.address_width,
                )
                self.submodules.cfu_port_debug_wb2axil = Wishbone2AXILite(
                    debug_wb, debug_axil32, base_address=0
                )

                debug_axi_full = AXIInterface(
                    data_width=hbm_slave.data_width,
                    address_width=hbm_slave.address_width,
                    id_width=hbm_slave.id_width,
                )
                self.submodules.cfu_port_debug_bridge = AXILite32toAXI256Bridge(
                    debug_axil32, debug_axi_full
                )
                masters.append(debug_axi_full)

            def _accept_all(_):
                return 1

            self.submodules.cfu_hbm_xbar = AXICrossbar(
                masters=masters,
                slaves=[(_accept_all, hbm_slave)],
            )
            print(
                f"[cfu-hbm] CFU AXI width {cfu_axi.data_width} -> HBM port {cfu_hbm_port} width {hbm_slave.data_width}"
            )

        else:
            if not self.integrated_main_ram_size:
                self.submodules.ddrphy = usddrphy.USPDDRPHY(
                    platform.request("ddram", ddram_channel),
                    memtype="DDR4",
                    cmd_latency=1,
                    sys_clk_freq=sys_clk_freq,
                    iodelay_clk_freq=600e6,
                    is_rdimm=True,
                )
                self.add_sdram(
                    "sdram",
                    phy=self.ddrphy,
                    module=MTA18ASF2G72PZ(sys_clk_freq, "1:4"),
                    size=0x40000000,
                    l2_cache_size=kwargs.get("l2_size", 8192),
                )
            self.add_ram("firmware_ram", 0x20000000, 0x8000)


def main():
    from litex.soc.integration.soc import LiteXSoCArgumentParser

    parser = LiteXSoCArgumentParser(description="U280 CFU-HBM AXI bridge target")
    tg = parser.add_argument_group(title="Target options")
    tg.add_argument("--build", action="store_true")
    tg.add_argument("--load", action="store_true")
    tg.add_argument("--sys-clk-freq", default=250e6, help="System clock frequency (Hz)")
    tg.add_argument("--ddram-channel", default="0")
    tg.add_argument("--with-hbm", action="store_true", help="Enable the HBM subsystem")
    tg.add_argument(
        "--cfu-hbm-port",
        type=int,
        default=0,
        help="HBM port index reserved for the CFU AXI master",
    )
    tg.add_argument(
        "--cpu-hbm-ports",
        default="1,2,3",
        help="Comma-separated HBM port indices exposed on the CPU bus",
    )
    tg.add_argument(
        "--expose-cfu-port-window",
        action="store_true",
        help="Expose the CFU port on the CPU bus for debug access",
    )
    tg.add_argument(
        "--cfu-port-window-base",
        default="0x48000000",
        help="Base address for the CFU debug window",
    )
    tg.add_argument(
        "--cfu-port-window-size",
        default="0x01000000",
        help="Size of the CFU debug window (bytes)",
    )
    builder_args(parser)
    soc_core_args(parser)
    args = parser.parse_args()
    if args.with_hbm:
        args.sys_clk_freq = 250e6

    cpu_ports = tuple(int(p, 0) for p in str(args.cpu_hbm_ports).split(",") if p)

    soc = BaseSoC(
        sys_clk_freq=int(float(args.sys_clk_freq)),
        ddram_channel=int(args.ddram_channel, 0),
        with_hbm=args.with_hbm,
        cfu_hbm_port=args.cfu_hbm_port,
        cpu_hbm_ports=cpu_ports,
        expose_cfu_port_window=args.expose_cfu_port_window,
        cfu_port_window_base=int(args.cfu_port_window_base, 0),
        cfu_port_window_size=int(args.cfu_port_window_size, 0),
        **soc_core_argdict(args),
    )
    builder = Builder(soc, **builder_argdict(args))
    if args.build:
        builder.build()
    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(builder.get_bitstream_filename(mode="sram"))


if __name__ == "__main__":
    main()
