"""
ProxmoxService — REST API client for Proxmox VE nodes.

Authentication: API token (PVEAPIToken=user@realm!tokenid=secret)
Requires: requests (already in requirements.txt or add it)

Coverage:
  - Node resource info (CPU, RAM, storage)
  - List VMs (qemu) and LXC containers
  - Start / stop / reboot VM or container
  - Create VM (qemu) with sensible defaults
  - Create LXC container
  - Delete VM or LXC
  - Status for a single VM/LXC
"""
import logging
import ssl
import urllib.request
import urllib.parse
import json
from typing import Optional

logger = logging.getLogger(__name__)


class ProxmoxError(Exception):
    pass


class ProxmoxService:
    """Thin HTTP client around the Proxmox VE API v2."""

    DEFAULT_TIMEOUT = 15

    def __init__(self, host: str, port: int, token_id: str, token_secret: str,
                 verify_ssl: bool = False):
        """
        :param host:         Hostname or IP of the Proxmox node
        :param port:         API port (default 8006)
        :param token_id:     Full token ID: "user@realm!tokenname"
        :param token_secret: Token UUID secret
        :param verify_ssl:   Verify TLS certificate (disable for self-signed)
        """
        self.base = f"https://{host}:{port}/api2/json"
        self.auth_header = f"PVEAPIToken={token_id}={token_secret}"
        self.verify_ssl = verify_ssl

    # ── Internal HTTP helpers ─────────────────────────────────────────────

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        url = f"{self.base}{path}"
        data = urllib.parse.urlencode(body).encode() if body else None
        ctx = ssl.create_default_context()
        if not self.verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        headers = {
            "Authorization": self.auth_header,
            "Content-Type": "application/x-www-form-urlencoded",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=self.DEFAULT_TIMEOUT) as resp:
                raw = resp.read().decode()
                result = json.loads(raw)
                return result.get("data", result)
        except urllib.error.HTTPError as exc:
            body_str = exc.read().decode("utf-8", errors="replace")
            try:
                msg = json.loads(body_str).get("errors") or body_str
            except Exception:
                msg = body_str
            raise ProxmoxError(f"HTTP {exc.code} — {msg}") from exc
        except urllib.error.URLError as exc:
            raise ProxmoxError(f"Connection failed: {exc.reason}") from exc
        except Exception as exc:
            raise ProxmoxError(f"Unexpected error: {exc}") from exc

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, body: Optional[dict] = None) -> dict:
        return self._request("POST", path, body or {})

    def _delete(self, path: str) -> dict:
        return self._request("DELETE", path)

    # ── Node resources ────────────────────────────────────────────────────

    def get_node_status(self, node: str) -> dict:
        """Return CPU, memory, and disk info for a node."""
        try:
            data = self._get(f"/nodes/{node}/status")
            mem = data.get("memory", {})
            cpu = data.get("cpu", 0)
            cores = data.get("cpuinfo", {}).get("cpus", 0)
            root_fs = data.get("rootfs", {})
            return {
                "status": "online",
                "cpu_load": round(float(cpu) * 100, 1),
                "cpu_cores": int(cores),
                "ram_used_mb": int(mem.get("used", 0)) // (1024 * 1024),
                "ram_total_mb": int(mem.get("total", 0)) // (1024 * 1024),
                "disk_used_gb": int(root_fs.get("used", 0)) // (1024 ** 3),
                "disk_total_gb": int(root_fs.get("total", 0)) // (1024 ** 3),
            }
        except ProxmoxError as exc:
            logger.warning("Proxmox get_node_status failed: %s", exc)
            return {
                "status": "offline",
                "cpu_load": 0, "cpu_cores": 0,
                "ram_used_mb": 0, "ram_total_mb": 0,
                "disk_used_gb": 0, "disk_total_gb": 0,
            }

    # ── Listing ───────────────────────────────────────────────────────────

    def list_vms(self, node: str) -> list:
        """List all QEMU VMs on the node."""
        try:
            return self._get(f"/nodes/{node}/qemu") or []
        except ProxmoxError as exc:
            logger.warning("Proxmox list_vms: %s", exc)
            return []

    def list_containers(self, node: str) -> list:
        """List all LXC containers on the node."""
        try:
            return self._get(f"/nodes/{node}/lxc") or []
        except ProxmoxError as exc:
            logger.warning("Proxmox list_containers: %s", exc)
            return []

    def list_all(self, node: str) -> list:
        """List all VMs + containers, tagged with their type."""
        vms = [{"type": "qemu", **v} for v in self.list_vms(node)]
        cts = [{"type": "lxc", **c} for c in self.list_containers(node)]
        combined = vms + cts
        # Normalise field names for the panel
        result = []
        for item in combined:
            result.append({
                "vmid":   item.get("vmid"),
                "name":   item.get("name", f"{item['type']}-{item.get('vmid')}"),
                "status": item.get("status", "unknown"),
                "type":   item.get("type"),
                "cpu":    item.get("cpus", item.get("cpu", 1)),
                "ram_mb": int(item.get("mem", item.get("maxmem", 0))) // (1024 * 1024),
                "disk_gb": int(item.get("disk", item.get("maxdisk", 0))) // (1024 ** 3),
                "uptime": item.get("uptime", 0),
            })
        return sorted(result, key=lambda x: x.get("vmid", 0))

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def _lifecycle(self, node: str, vmid: int, vtype: str, action: str) -> dict:
        endpoint = "qemu" if vtype == "qemu" else "lxc"
        try:
            result = self._post(f"/nodes/{node}/{endpoint}/{vmid}/status/{action}")
            return {"success": True, "task": result}
        except ProxmoxError as exc:
            return {"success": False, "error": str(exc)}

    def start(self, node: str, vmid: int, vtype: str = "qemu") -> dict:
        return self._lifecycle(node, vmid, vtype, "start")

    def stop(self, node: str, vmid: int, vtype: str = "qemu") -> dict:
        return self._lifecycle(node, vmid, vtype, "stop")

    def reboot(self, node: str, vmid: int, vtype: str = "qemu") -> dict:
        return self._lifecycle(node, vmid, vtype, "reboot")

    def shutdown(self, node: str, vmid: int, vtype: str = "qemu") -> dict:
        """Graceful ACPI shutdown."""
        return self._lifecycle(node, vmid, vtype, "shutdown")

    # ── VM/Container status ───────────────────────────────────────────────

    def get_vm_status(self, node: str, vmid: int, vtype: str = "qemu") -> dict:
        endpoint = "qemu" if vtype == "qemu" else "lxc"
        try:
            data = self._get(f"/nodes/{node}/{endpoint}/{vmid}/status/current")
            return {
                "vmid": vmid,
                "status": data.get("status", "unknown"),
                "cpu_pct": round(float(data.get("cpu", 0)) * 100, 1),
                "ram_used_mb": int(data.get("mem", 0)) // (1024 * 1024),
                "ram_total_mb": int(data.get("maxmem", 0)) // (1024 * 1024),
                "uptime": data.get("uptime", 0),
                "netin": data.get("netin", 0),
                "netout": data.get("netout", 0),
            }
        except ProxmoxError as exc:
            return {"vmid": vmid, "status": "error", "error": str(exc)}

    # ── Create VM ─────────────────────────────────────────────────────────

    def create_vm(self, node: str, vmid: int, name: str,
                  ram_mb: int = 1024, cores: int = 1, disk_gb: int = 20,
                  iso: Optional[str] = None, storage: str = "local-lvm",
                  net_bridge: str = "vmbr0") -> dict:
        """
        Create a QEMU VM with the given specs.
        ISO should be in format "local:iso/filename.iso".
        """
        params: dict = {
            "vmid": vmid,
            "name": name,
            "memory": ram_mb,
            "cores": cores,
            "sockets": 1,
            "net0": f"virtio,bridge={net_bridge}",
            "scsi0": f"{storage}:{disk_gb}",
            "scsihw": "virtio-scsi-pci",
            "boot": "order=scsi0;ide2;net0",
            "agent": "enabled=1",
        }
        if iso:
            params["ide2"] = f"{iso},media=cdrom"
        try:
            result = self._post(f"/nodes/{node}/qemu", params)
            return {"success": True, "task": result}
        except ProxmoxError as exc:
            return {"success": False, "error": str(exc)}

    # ── Create LXC ────────────────────────────────────────────────────────

    def create_lxc(self, node: str, vmid: int, hostname: str,
                   template: str, ram_mb: int = 512, cores: int = 1,
                   disk_gb: int = 8, storage: str = "local-lvm",
                   password: Optional[str] = None,
                   net_bridge: str = "vmbr0") -> dict:
        """
        Create an LXC container.
        template should be e.g. "local:vztmpl/ubuntu-22.04-standard_22.04-1_amd64.tar.zst"
        """
        params: dict = {
            "vmid": vmid,
            "hostname": hostname,
            "ostemplate": template,
            "memory": ram_mb,
            "cores": cores,
            "rootfs": f"{storage}:{disk_gb}",
            "net0": f"name=eth0,bridge={net_bridge},ip=dhcp",
            "unprivileged": 1,
            "features": "nesting=1",
            "start": 0,
        }
        if password:
            params["password"] = password
        try:
            result = self._post(f"/nodes/{node}/lxc", params)
            return {"success": True, "task": result}
        except ProxmoxError as exc:
            return {"success": False, "error": str(exc)}

    # ── Delete ────────────────────────────────────────────────────────────

    def delete_vm(self, node: str, vmid: int) -> dict:
        try:
            result = self._delete(f"/nodes/{node}/qemu/{vmid}")
            return {"success": True, "task": result}
        except ProxmoxError as exc:
            return {"success": False, "error": str(exc)}

    def delete_lxc(self, node: str, vmid: int) -> dict:
        try:
            result = self._delete(f"/nodes/{node}/lxc/{vmid}")
            return {"success": True, "task": result}
        except ProxmoxError as exc:
            return {"success": False, "error": str(exc)}

    # ── Storage helpers ───────────────────────────────────────────────────

    def list_storage(self, node: str) -> list:
        """List available storage pools."""
        try:
            return self._get(f"/nodes/{node}/storage") or []
        except ProxmoxError:
            return []

    def list_isos(self, node: str, storage: str = "local") -> list:
        """List ISO images in a storage pool."""
        try:
            items = self._get(f"/nodes/{node}/storage/{storage}/content") or []
            return [i for i in items if i.get("content") == "iso"]
        except ProxmoxError:
            return []

    def list_templates(self, node: str, storage: str = "local") -> list:
        """List LXC templates in a storage pool."""
        try:
            items = self._get(f"/nodes/{node}/storage/{storage}/content") or []
            return [i for i in items if i.get("content") == "vztmpl"]
        except ProxmoxError:
            return []

    # ── Factory / health check ────────────────────────────────────────────

    @classmethod
    def from_node_record(cls, node) -> "ProxmoxService":
        """Build a ProxmoxService from a Node ORM object."""
        return cls(
            host=node.address,
            port=node.port or 8006,
            token_id=node.proxmox_token_id or "",
            token_secret=node.proxmox_token_secret or "",
            verify_ssl=False,
        )

    def ping(self, node_name: str) -> bool:
        """Quick health check — returns True if node responds."""
        try:
            self._get(f"/nodes/{node_name}/status")
            return True
        except ProxmoxError:
            return False
