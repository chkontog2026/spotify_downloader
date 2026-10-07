import os
import subprocess
from pathlib import Path
import spotdl_gui_v4 as gui

def open_path(path):
    subprocess.run(
        ["xdg-open", str(Path(path).expanduser().resolve())],
        check=True,
    )

# Συμβατότητα των κουμπιών ανοίγματος αρχείων με Ubuntu.
os.startfile = open_path

# Τα εικονίδια .ico χρησιμοποιούνται μόνο στα Windows.
original_resource_path = gui.resource_path
def resource_path(name):
    if name == "spotdl_gui_icon.ico":
        return Path("/nonexistent/spotdl_gui_icon.ico")
    return original_resource_path(name)
gui.resource_path = resource_path

gui.SpotDLApp().mainloop()
