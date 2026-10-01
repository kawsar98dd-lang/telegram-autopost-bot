"""Builds the LicenseManager for the web and worker processes."""

from __future__ import annotations

from .. import __version__
from ..config import Settings
from ..security.crypto import Cipher
from . import constants
from .client import LicenseClient
from .manager import LicenseManager
from .store import DbLicenseStore, get_or_create_installation_id


class LicenseSetupError(RuntimeError):
    pass


async def build_license_manager(settings: Settings, db, cipher: Cipher) -> LicenseManager:
    installation_id = await get_or_create_installation_id(db)
    server_url = settings.license_server_url_override or constants.LICENSE_SERVER_URL
    client = None
    if settings.license_enforcement:
        if not server_url or not constants.LICENSE_PUBLIC_KEY:
            raise LicenseSetupError(
                "This build has no license server configured. The seller must run "
                "license_server/keygen.py --write-constants before packaging."
            )
        client = LicenseClient(server_url, allow_insecure=not settings.is_production)
    manager = LicenseManager(
        store=DbLicenseStore(db, cipher),
        client=client,
        public_key=constants.LICENSE_PUBLIC_KEY,
        product=constants.PRODUCT_ID,
        installation_id=installation_id,
        host=settings.app_host,
        app_version=__version__,
        enforcement=settings.license_enforcement,
    )
    await manager.load()
    return manager
