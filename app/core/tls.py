import logging
import os
from dataclasses import dataclass
from typing import Optional


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TLSServerFiles:
    certfile: Optional[str]
    keyfile: Optional[str]


def resolve_tls_server_files(
    *,
    use_self_signed: bool,
    certfile: Optional[str],
    keyfile: Optional[str],
    component_name: str,
) -> TLSServerFiles:
    if not use_self_signed:
        logger.info(
            "USE_SELF_SIGNED_CERT is disabled; local TLS is not started and external proxy TLS termination is expected"
        )
        return TLSServerFiles(certfile=None, keyfile=None)

    missing_files = [path for path in (certfile, keyfile) if not path or not os.path.exists(path)]
    if missing_files:
        logger.error(
            "Invalid %s TLS configuration. Missing files: %s",
            component_name,
            ", ".join(missing_files),
        )
        raise RuntimeError(f"{component_name} TLS files are missing: {', '.join(missing_files)}")

    return TLSServerFiles(certfile=certfile, keyfile=keyfile)
