"""Setup for pypi package"""

import os
import codecs
from setuptools import setup, find_packages

here = os.path.abspath(os.path.dirname(__file__))

with codecs.open(os.path.join(here, "README.md"), encoding="utf-8") as fh:
    long_description = "\n" + fh.read()

# The release workflow sets LIB_VERSION from the tag. Without it setuptools gets
# None and refuses to build, so local builds fall back to a dev placeholder.
VERSION = os.getenv("LIB_VERSION", "0.0.0.dev0")
DESCRIPTION = "Bluetti BT"

# Setting up
setup(
    name="bluetti-bt-lib",
    version=VERSION,
    author="Patrick762",
    author_email="<pip-bluetti-bt-lib@hosting-rt.de>",
    description=DESCRIPTION,
    long_description_content_type="text/markdown",
    long_description=long_description,
    url="https://github.com/Patrick762/bluetti-bt-lib",
    packages=find_packages(),
    # match statements and X | None annotations need 3.10, and the readers mix
    # `except TimeoutError` with `except asyncio.TimeoutError`, which only names
    # one class from 3.11 on, where asyncio.TimeoutError is an alias of it.
    python_requires=">=3.11",
    install_requires=[
        "async_timeout",
        "bleak",
        "bleak_retry_connector",
        "crcmod",
        "cryptography",
        "pyasn1",
        # utils.bleak_client_mock needs Buffer, which is in the standard
        # library from 3.12 and in typing_extensions from 4.6.0. It resolved
        # before only because bleak asks for it on the same versions.
        'typing_extensions>=4.6.0; python_version < "3.12"',
    ],
    keywords=[],
    entry_points={
        "console_scripts": [
            "bluetti-scan = bluetti_bt_lib.scripts.bluetti_scan:start",
            "bluetti-detect = bluetti_bt_lib.scripts.bluetti_detect:start",
            "bluetti-read = bluetti_bt_lib.scripts.bluetti_read:start",
            "bluetti-readall = bluetti_bt_lib.scripts.bluetti_readall:start",
            "bluetti-write = bluetti_bt_lib.scripts.bluetti_write:start",
            "bluetti-parse = bluetti_bt_lib.scripts.bluetti_parse:start",
        ],
    },
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
    ],
)
