"""Builds the LicenseManager for the web and worker processes."""

from __future__ import annotations

from .. import __version__
from ..config import Settings
from ..security.crypto import Cipher
from . import constants
from .client import LicenseClient
from .manager import LicenseManager
from .offline import OfflineLicenseManager
from .protocol import b64url_decode
from .store import DbLicenseStore, get_or_create_installation_id


class LicenseSetupError(RuntimeError):
    pass


def _public_key_ok(value: str) -> bool:
    try:
        return len(b64url_decode(value)) == 32
    except ValueError:
        return False


async def build_license_manager(settings: Settings, db, cipher: Cipher):
    """Choose the license mode. The result is never "silently unlicensed" in production:

    * enforcement off  -> development/testing only (config.py refuses it in production; checked again here);
    * enforcement on, build has a public key and NO license server URL -> OFFLINE signed license file (the default product mode);
    * enforcement on, build has a public key AND a license server URL -> the online activation protocol (kept, optional);
    * enforcement on and no (valid) public key in the build -> refuse to start with an actionable message.
    """
    if not settings.license_enforcement:
        if settings.is_production:
            raise LicenseSetupError("License enforcement cannot be disabled in production (APP_ENV=production).")
        return LicenseManager(store=DbLicenseStore(db, cipher), client=None, public_key=constants.LICENSE_PUBLIC_KEY,
                              product=constants.PRODUCT_ID, installation_id=await get_or_create_installation_id(db),
                              host=settings.app_host, app_version=__version__, enforcement=False)
    if not _public_key_ok(constants.LICENSE_PUBLIC_KEY):
        raise LicenseSetupError(
            "This build contains no valid license public key, so no license can be verified. Customers: contact the seller "
            "for a correctly packaged build. Seller: run 'python license_server/keygen.py --private-out <file outside the "
            "project> --write-constants app/licensing/constants.py' once, then package again (docs/LICENSING.md). "
            "For local development only: APP_ENV=development and LICENSE_ENFORCEMENT=false.")
    server_url = settings.license_server_url_override or constants.LICENSE_SERVER_URL
    if not server_url:
        manager = OfflineLicenseManager(
            public_key=constants.LICENSE_PUBLIC_KEY, product=constants.PRODUCT_ID, host=settings.app_host,
            file_path=settings.license_file, inline_content=settings.license_file_content, enforcement=True)
        await manager.load()
        return manager
    manager = LicenseManager(
        store=DbLicenseStore(db, cipher), client=LicenseClient(server_url, allow_insecure=not settings.is_production),
        public_key=constants.LICENSE_PUBLIC_KEY, product=constants.PRODUCT_ID,
        installation_id=await get_or_create_installation_id(db), host=settings.app_host, app_version=__version__,
        enforcement=True)
    await manager.load()
    return manager
