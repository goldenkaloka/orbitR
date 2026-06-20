import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from decouple import config

mock = config('MOCK_SMS', default='True')
print(f"MOCK_SMS value: '{mock}' (type: {type(mock).__name__})")
print(f"Boolean check: {mock == 'True'}")