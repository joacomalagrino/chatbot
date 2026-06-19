"""Hace que la raíz del proyecto esté en sys.path para que los tests puedan
importar `services.*` sin instalar el paquete."""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
