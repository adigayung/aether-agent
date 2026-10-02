import sys

print("sys.executable:", sys.executable)
print("sys.prefix   :", sys.prefix)
print("in venv      :", sys.prefix != sys.base_prefix)
