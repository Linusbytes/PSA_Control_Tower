"""Data models for the port and PSCH MCC (multi-country consolidation) flow.

Two facilities, one cargo story: MCC cargo arrives at Tuas inside inbound
containers, is deconsolidated at the PSA Supply Chain Hub (PSCH), stored in
bins, then re-consolidated into outbound containers that must make a specific
vessel's loading plan. Every record here is synthetic but modelled on realistic
port/CFS structures.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class CargoFlag(str, Enum):
    IMPORT = "import"
    EXPORT = "export"
    TRANSSHIPMENT = "transshipment"
    DECONSOLIDATION_REQUIRED = "deconsolidation_required"


class CustomsStatus(str, Enum):
    CLEARED = "cleared"
    PENDING = "pending"
    HELD = "held"


class ServiceType(str, Enum):
    LCL_DECONSOLIDATION = "LCL_deconsolidation"
    MCC_CONSOLIDATION = "MCC_consolidation"
    LCL_CONSOLIDATION = "LCL_consolidation"
    TRANSLOADING = "transloading"


class StorageZone(str, Enum):
    AMBIENT = "ambient"
    COLD_ROOM = "cold_room"
    HAZMAT = "hazmat"


class CargoType(str, Enum):
    """Physical cargo type of a palletised shipment (ambient / reefer / hazmat)."""

    AMBIENT = "ambient"
    REEFER = "reefer"
    HAZMAT = "hazmat"


class DockSlotStatus(str, Enum):
    UNASSIGNED = "unassigned"
    ASSIGNED = "assigned"
    OCCUPIED = "occupied"


class VesselStatus(str, Enum):
    DOCKED = "docked"    # alongside a berth now
    INBOUND = "inbound"  # at sea, booked for a berth, not yet alongside
    DEPARTED = "departed"


class JourneyStatus(str, Enum):
    """Lifecycle of an inbound MCC container from vessel to PSCH doorstep."""

    EN_ROUTE_SEA = "En Route (Sea)"    # on the vessel, still at sea
    UNLOADED = "Unloaded"              # discharged at the quay
    AT_DEPOT = "Depot"                 # sitting in the port depot yard
    EN_ROUTE_ROAD = "En Route (Road)"  # on a prime mover to PSCH
    ARRIVED = "Arrived"                # at the PSCH doorstep, receiving begun


class OutboundStatus(str, Enum):
    """Lifecycle of a consolidated export container leaving PSCH."""

    STAGED = "staged"        # being stuffed / awaiting release
    RELEASED = "released"    # lane released, container dispatched
    IN_TRANSIT = "in_transit"  # on the road back to the port
    LOADED = "loaded"        # arrived at the quay loading area, loaded


class Container(BaseModel):
    """Port-side record (§5.1): a physical container on a voyage."""

    container_id: str
    voyage_id: str
    status: str = "discharged"  # free-text operational status
    discharge_timestamp: datetime | None = None
    yard_location: str | None = None
    size_type: str  # "20FT" | "40FT" | "40HC" (20ft or 40ft only; 98% are 40ft)
    cargo_flag: CargoFlag
    customs_status: CustomsStatus
    consignee_id: str | None = None
    special_handling: list[str] = Field(default_factory=list)  # reefer/hazmat/oversized
    vessel_cutoff: datetime | None = None  # for export-bound containers
    # MCC tracking: the exact cell the container occupies in the carrying
    # vessel's stowage plan, in industry Bay-Row-Tier notation
    # (e.g. "Bay 33(34) · Row 08 · Tier 86"). Structured fields drive the
    # bay-plan visualisation; stow_position is the human-readable form.
    stow_position: str | None = None
    stow_bay: int | None = None   # bay number (odd = 20ft, even = 40ft)
    stow_row: int | None = None   # stack / row number, 1..16
    stow_tier: int | None = None  # tier (02..18 hold, 82..92 above deck)


class YardStatus(BaseModel):
    """Yard-level record (§5.1)."""

    block: str
    zone: str
    utilization_pct: float  # 0-100
    gate_lanes_available: int


class Vessel(BaseModel):
    """Marine-side record: a vessel on a voyage with a berth assignment.

    Berths are the terminal's quay rectangles; a berth may be occupied by a
    docked vessel, booked by an inbound one, or free. Inbound vessels carry
    live tracking fields (distance from Tuas, speed) so the ship-tracker view
    can show how far away the cargo is and how fast it is closing.
    """

    voyage_id: str
    vessel_name: str
    status: VesselStatus
    berth_id: str | None = None  # docked -> alongside; inbound -> planned berth
    eta: datetime | None = None
    etd: datetime | None = None
    moves_planned: int | None = None  # TEU expected to work this call
    destination: str | None = None  # next port of call (outbound cargo target)
    distance_nm: float | None = None  # nautical miles from Tuas (0 when docked)
    speed_knots: float | None = None  # current speed over ground


class Booking(BaseModel):
    """PSCH-side record (§5.2): the service booked for one container."""

    booking_id: str
    linked_container_id: str  # join key back to Container
    service_type: ServiceType
    shipper_ids: list[str] = Field(default_factory=list)
    required_by: datetime
    storage_zone: StorageZone
    dock_slot_status: DockSlotStatus = DockSlotStatus.UNASSIGNED
    processing_queue_position: int = 0
    destination: str | None = None  # export destination for MCC/LCL cargo


class DrayageStatus(BaseModel):
    """Drayage capacity (§5.3)."""

    total_trucks: int
    available_trucks: int

    @property
    def utilization_pct(self) -> float:
        if self.total_trucks <= 0:
            return 0.0
        return round(100 * (self.total_trucks - self.available_trucks) / self.total_trucks, 1)


class SlaProfile(BaseModel):
    """Customer service commitment used to prioritise staging/putaway."""

    consignee_id: str
    priority_tier: int  # 1 (highest) .. 3
    sla_hours: int


class Shipment(BaseModel):
    """A palletised cargo unit deconsolidated at PSCH, awaiting consolidation."""

    shipment_id: str
    shipper_id: str
    destination: str
    cargo_type: CargoType
    volume_cbm: float
    ready_time: datetime
    service_type: ServiceType
    consignee_id: str | None = None
    source_container_id: str | None = None  # inbound container it came from
    outbound_container_id: str | None = None  # consolidated container it feeds


class MccPlan(BaseModel):
    """Agent output: the end-to-end plan for one inbound MCC container.

    The agent derives every stage time from the carrying vessel's ETA (sea
    arrival -> quay unload -> depot -> road dispatch -> PSCH doorstep), then
    plans the PSCH receiving area, robot putaway bin, and the consolidation
    steps (staging, move, pallet pick, lane release) for the cargo inside.
    """

    container_id: str
    carrying_vessel_id: str
    carrying_vessel_name: str
    vessel_destination: str | None = None
    vessel_distance_nm: float | None = None
    vessel_speed_knots: float | None = None
    stow_position: str | None = None

    # Journey stages (derived from the vessel ETA).
    sea_arrival: datetime            # vessel alongside at the berth
    unload_end: datetime             # container discharged
    depot_arrive: datetime           # in the port depot yard
    road_depart: datetime            # dispatched on a prime mover
    psch_receipt_eta: datetime       # ETA at the PSCH doorstep

    # PSCH receiving & putaway plan (planned before the container arrives).
    receiving_area: str              # e.g. "RA-2 · Door D3"
    staging_start: datetime          # wait time at the receiving staging area
    staging_end: datetime
    move_start: datetime             # start of the move to the bin
    move_end: datetime
    bin_location: str                # robot putaway destination, e.g. "Bin 1-12-2A"
    #                                  (DC convention: AISLE-LEVEL-BAY = Aisle 1,
    #                                  Level 12, Bay 2A; aisles 1-24, ambient 1-21
    #                                  (21 hazmat), cold room 22-24)
    putaway_robot: str               # e.g. "Robot 04"
    pallet_pick_time: datetime       # when the pallets are picked for consolidation
    release_lane: str                # lane released for this container number
    consolidation_group: str | None  # outbound container id this cargo feeds
    reasoning: str = ""


class OutboundContainer(BaseModel):
    """A consolidated export container stuffed at PSCH, bound for a vessel.

    The cargo of several inbound MCC containers (multi-country consolidation)
    is pallet-picked and stuffed into this container, which then returns to the
    port to be loaded onto the vessel whose berth rectangle pops up on the map.
    """

    container_id: str
    destination: str
    size_type: str = "40HC"  # consolidation containers are 40ft-family (ISO 6346)
    source_container_ids: list[str] = Field(default_factory=list)
    source_shipment_ids: list[str] = Field(default_factory=list)
    bound_vessel_id: str
    bound_vessel_name: str
    vessel_etd: datetime | None = None  # when the vessel leaves the port
    stow_position: str | None = None    # exact cell on the vessel (Bay-Row-Tier)
    stow_bay: int | None = None         # loading cell: bay (odd/even parity)
    stow_row: int | None = None         # loading cell: stack / row, 1..16
    stow_tier: int | None = None        # loading cell: tier (02..18 hold, 82..92 deck)
    stuffing_start: datetime            # pallet pick / stuffing begins
    stuffing_end: datetime
    lane_release_time: datetime         # loading lane released for this container
    loading_lane: str
    road_depart: datetime               # leaves PSCH for the port
    eta_loading_area: datetime          # ETA at the quay loading area for loading
    status: OutboundStatus = OutboundStatus.STAGED
    # PSCH releasing-lane allocation: the physical lane, or contiguous group of
    # adjacent lanes (numbered 1..26), where this group's pallets are staged in
    # the dispatch area waiting to be loaded into this consolidation container.
    # The agent assigns one lane per group when volume is small, or a span of
    # neighbouring lanes when many pallets are staged for one container.
    staging_lane_start: int | None = None  # first releasing lane, 1..26
    staging_lane_end: int | None = None    # last releasing lane (inclusive)
    reasoning: str = ""
