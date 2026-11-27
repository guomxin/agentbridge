"""Model subprocesses use only the central library bundled with their own release."""
import os
from pathlib import Path
import stat


BOOTSTRAP = """import runpy,sys
from pathlib import Path
sys.path[:0]=sys.argv[1:3]
import agentbridge
if not str(Path(agentbridge.__file__)).startswith(str(Path(sys.argv[2]))+'/agentbridge/'):
    raise ImportError('Original release library required')
runpy.run_module('nanobot_agentbridge.worker',run_name='__main__')
"""


def release_library(release):
    wheel = Path(release) / 'agentbridge.whl'
    info = wheel.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {os.getuid(), 0}
            or info.st_mode & 0o022):
        raise PermissionError('Original release library unavailable')
    return wheel
