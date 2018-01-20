from . import dataset
from . import network
from . import utils

def info():
    print("""
--- INFO ---
    Neural Networks tools for OMTF@CMS
    Jacek Łysiak 2018
    Due to high overhead with installing ROOT and its python packages
    utility methods which uses ROOT are not imported by default.
    Call 'import_root_utils()' method to import them.
--- END OF INFO ---
    """)

def import_root_utils():
    """Import part of package which uses ROOT."""
    from . import root_utils

# Show info...
info()