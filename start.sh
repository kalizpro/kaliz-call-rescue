#!/bin/bash

# Script para configurar el entorno virtual e iniciar call-rescue
# Autor: Script automatizado

echo "==================================="
echo "   SETUP Y EJECUCIÓN - Call Rescue"
echo "==================================="
echo ""

# Eliminar el entorno virtual si existe
if [ -d "venv" ]; then
    echo "→ Eliminando entorno virtual existente..."
    rm -rf venv
    echo "✓ Entorno virtual eliminado"
fi

# Crear nuevo entorno virtual
echo ""
echo "→ Creando nuevo entorno virtual..."
python3 -m venv venv

# Verificar si el venv se creó correctamente
if [ ! -d "venv" ]; then
    echo "✗ Error: No se pudo crear el entorno virtual"
    exit 1
fi
echo "✓ Entorno virtual creado"

# Activar el entorno virtual
echo ""
echo "→ Activando entorno virtual..."
source venv/bin/activate

# Actualizar pip (opcional pero recomendado)
echo ""
echo "→ Actualizando pip..."
pip install --upgrade pip --quiet

# Instalar dependencias desde requirements.txt
echo ""
echo "→ Instalando dependencias desde requirements.txt..."
pip install -r requirements.txt

# Verificar si la instalación fue exitosa
if [ $? -ne 0 ]; then
    echo "✗ Error al instalar las dependencias"
    exit 1
fi
echo "✓ Dependencias instaladas correctamente"

# Ejecutar el script principal
echo ""
echo "==================================="
echo "   INICIANDO APLICACIÓN"
echo "==================================="
echo ""
python run.py
