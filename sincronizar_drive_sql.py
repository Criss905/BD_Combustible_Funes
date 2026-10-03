import os
import sys
import uuid
import gspread
import psycopg2
import psycopg2.extras
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

# ==========================================
# 1. CONFIGURACIÓN CLOUD NATIVE Y CONSTANTES
# ==========================================
load_dotenv()
console = Console()

# Asegúrate de configurar este ID en tus secretos o .env en el futuro
ID_GOOGLE_SHEET = os.getenv("ID_GOOGLE_SHEET", "1B3X4oKZ-BAdC_Rp4b9RNbeO-jzOHdrRYqiIYNcB5vq4")

DB_CONFIG = {
    'dbname': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT')
}

def conectar_google_sheets():
    console.print("[cyan]Conectando a Google Workspace (Zero Trust)...[/cyan]")
    alcance = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    try:
        credenciales = ServiceAccountCredentials.from_json_keyfile_name("credenciales.json", alcance)
        cliente = gspread.authorize(credenciales)
        return cliente.open_by_key(ID_GOOGLE_SHEET)
    except Exception as e:
        raise ValueError(f"Fallo en credenciales de Drive: {e}")

def limpiar_string(valor):
    """Convierte cadenas vacías de Google Sheets a None (NULL en SQL)"""
    if valor is None:
        return None
    val_str = str(valor).strip()
    return val_str if val_str != "" else None

def formatear_fecha(fecha_cruda):
    """Intenta convertir fechas latinas (DD/MM/YYYY) al estándar ISO de PostgreSQL"""
    if not fecha_cruda:
        return None
    try:
        return datetime.strptime(str(fecha_cruda).strip(), '%d/%m/%Y').strftime('%Y-%m-%d')
    except ValueError:
        return str(fecha_cruda).strip()

def ejecutar_sincronizacion():
    console.print(Panel.fit("[bold blue]🔄 SINCRONIZADOR CLOUD (APPSHEET -> SUPABASE)[/bold blue]", subtitle="Operaciones Batch + Integridad Estricta"))
    
    if not all([os.getenv('DB_HOST'), os.getenv('DB_PASS')]):
        console.print("[bold red]❌ Error: Faltan credenciales de base de datos.[/bold red]")
        sys.exit(1)

    try:
        hoja = conectar_google_sheets()
        
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                
                # --- A. SINCRONIZAR CONTRATOS (BATCH) ---
                console.print("Sincronizando [bold]Dim_Contratos[/bold]...")
                records_c = hoja.worksheet("Dim_Contratos").get_all_records()
                
                # Filtrar filas completamente vacías y preparar tuplas
                valores_c = []
                for r in records_c:
                    id_c = limpiar_string(r.get('id_contrato'))
                    if id_c:  # Solo procesa si hay un ID válido
                        presupuesto = r.get('presupuesto_total')
                        # Asegurar que el presupuesto sea un número flotante válido
                        presupuesto = float(presupuesto) if presupuesto not in ["", None] else 0.0
                        valores_c.append((
                            id_c,
                            limpiar_string(r.get('bolsa')),
                            limpiar_string(r.get('estado')) or 'ACTIVO',
                            presupuesto
                        ))
                
                if valores_c:
                    query_c = """
                        INSERT INTO dim_contratos (id_contrato, bolsa, estado, presupuesto_total)
                        VALUES %s
                        ON CONFLICT (id_contrato) DO UPDATE SET
                            bolsa = EXCLUDED.bolsa,
                            estado = EXCLUDED.estado,
                            presupuesto_total = EXCLUDED.presupuesto_total;
                    """
                    psycopg2.extras.execute_values(cursor, query_c, valores_c)
                    conn.commit()
                    console.print(f"[green] ✅ Contratos sincronizados: {len(valores_c)}[/green]")

                # --- B. SINCRONIZAR VEHÍCULOS (BATCH) ---
                console.print("Sincronizando [bold]Dim_Vehiculos[/bold]...")
                records_v = hoja.worksheet("Dim_Vehiculos").get_all_records()
                
                valores_v = []
                for r in records_v:
                    placa = limpiar_string(r.get('placa'))
                    if placa:
                        valores_v.append((
                            placa.upper(),
                            limpiar_string(r.get('tipo_vehiculo')),
                            limpiar_string(r.get('estado')) or 'ACTIVO',
                            limpiar_string(r.get('bolsa'))
                        ))
                
                if valores_v:
                    query_v = """
                        INSERT INTO dim_vehiculos (placa, tipo_vehiculo, estado, bolsa)
                        VALUES %s
                        ON CONFLICT (placa) DO UPDATE SET
                            tipo_vehiculo = EXCLUDED.tipo_vehiculo,
                            estado = EXCLUDED.estado,
                            bolsa = EXCLUDED.bolsa;
                    """
                    psycopg2.extras.execute_values(cursor, query_v, valores_v)
                    conn.commit()
                    console.print(f"[green] ✅ Vehículos sincronizados: {len(valores_v)}[/green]")

                # --- C. SINCRONIZAR VALES (BATCH) ---
                console.print("Sincronizando [bold]Fact_Vales[/bold]...")
                records_val = hoja.worksheet("Fact_Vales").get_all_records()
                
                valores_val = []
                for r in records_val:
                    # Garantizar que el vale tenga una placa asociada
                    placa = limpiar_string(r.get('placa'))
                    if placa:
                        # Si el id_vale viene vacío de AppSheet, generar uno real y seguro
                        id_vale = limpiar_string(r.get('id_vale'))
                        if not id_vale:
                            id_vale = str(uuid.uuid4())[:8]
                            
                        num_fisico = r.get('num_vale_fisico')
                        num_fisico = int(num_fisico) if num_fisico not in ["", None] else None
                        
                        gals = r.get('galones_autorizados')
                        gals = float(gals) if gals not in ["", None] else 0.0

                        valores_val.append((
                            id_vale,
                            num_fisico,
                            formatear_fecha(r.get('fecha_emision')),
                            placa.upper(),
                            limpiar_string(r.get('id_contrato')),
                            gals
                        ))

                if valores_val:
                    query_val = """
                        INSERT INTO fact_vales (id_vale, num_vale_fisico, fecha_emision, placa, id_contrato, galones_autorizados)
                        VALUES %s
                        ON CONFLICT (id_vale) DO UPDATE SET
                            num_vale_fisico = EXCLUDED.num_vale_fisico,
                            fecha_emision = EXCLUDED.fecha_emision,
                            placa = EXCLUDED.placa,
                            id_contrato = EXCLUDED.id_contrato,
                            galones_autorizados = EXCLUDED.galones_autorizados;
                    """
                    psycopg2.extras.execute_values(cursor, query_val, valores_val)
                    conn.commit()
                    console.print(f"[green] ✅ Vales procesados: {len(valores_val)}[/green]")

        console.print("\n[bold green]💾 Sincronización exitosa. Base de datos Cloud actualizada mediante transacciones en bloque.[/bold green]")

    except Exception as e:
        console.print(f"\n[bold red]❌ ERROR CRÍTICO DE CONEXIÓN O EJECUCIÓN: {e}[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    ejecutar_sincronizacion()