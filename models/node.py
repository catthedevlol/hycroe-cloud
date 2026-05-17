from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float
from sqlalchemy.orm import relationship
from datetime import datetime
from .base import Base


class Node(Base):
    __tablename__ = "nodes"

    id               = Column(Integer, primary_key=True)
    name             = Column(String, unique=True, index=True, nullable=False)
    display_name     = Column(String, nullable=True)
    address          = Column(String, nullable=False)          # IP / hostname
    port             = Column(Integer, default=8443)
    # Incus fields
    incus_remote     = Column(String, nullable=True)           # Incus remote name
    cert_fingerprint = Column(String, nullable=True)
    auth_token       = Column(String, nullable=True)
    # Proxmox fields
    node_type        = Column(String, default="incus")         # "incus" or "proxmox"
    proxmox_node     = Column(String, nullable=True)           # PVE node name (e.g. "pve")
    proxmox_token_id = Column(String, nullable=True)           # "user@realm!tokenname"
    proxmox_token_secret = Column(String, nullable=True)       # Token UUID
    # Resources (populated by refresh)
    status           = Column(String, default="unknown")       # online/offline/maintenance
    cpu_cores        = Column(Integer, default=0)
    cpu_load         = Column(Float, default=0.0)
    ram_total_mb     = Column(Integer, default=0)
    ram_used_mb      = Column(Integer, default=0)
    disk_total_gb    = Column(Integer, default=0)
    disk_used_gb     = Column(Integer, default=0)
    max_vps          = Column(Integer, default=50)
    is_default       = Column(Boolean, default=False)
    maintenance      = Column(Boolean, default=False)
    location         = Column(String, nullable=True)
    tags             = Column(String, nullable=True)
    last_seen        = Column(DateTime, nullable=True)
    added_at         = Column(DateTime, default=datetime.utcnow)

    vps_list = relationship("VPS", back_populates="node")

    @property
    def is_proxmox(self) -> bool:
        return self.node_type == "proxmox"

    @property
    def is_incus(self) -> bool:
        return self.node_type != "proxmox"
