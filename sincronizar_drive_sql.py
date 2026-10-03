import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import psycopg2
import pandas as pd
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
import uuid
from datetime import datetime # <- NUEVA LIBRERÍA DE TRADUCCIÓN

# ==========================================
# 1. CONFIGURACIÓN CLOUD NATIVE
# ==========================================
load_dotenv()
console = Console()

# ID único de tu documento de Google Sheets
ID_GOOGLE_SHEET = "1B3X4oKZ-BAdC_Rp4b9RNbeO-jzOHdrRYqiIYNcB5vq4"

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
        raise ValueError(f"Fallo en credenciales.json o permisos de Drive: {e}")

def ejecutar_sincronizacion():
    console.print(Panel.fit("[bold blue]🔄 SINCRONIZADOR CLOUD (APPSHEET -> SUPABASE)[/bold blue]", subtitle="Tolerancia a Fallos + Context Managers"))
    
    if not os.getenv('DB_HOST'):
        console.print("[bold red]❌ Error: Faltan credenciales de PostgreSQL en el archivo .env[/bold red]")
        return

    try:
        hoja = conectar_google_sheets()
        
        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                
                # --- A. SINCRONIZAR CONTRATOS ---
                console.print("Sincronizando [bold]Dim_Contratos[/bold]...")
                df_c = pd.DataFrame(hoja.worksheet("Dim_Contratos").get_all_records())
                ok_c = 0
                for _, fila in df_c.iterrows():
                    try:
                        cursor.execute("""
                            INSERT INTO dim_contratos (id_contrato, bolsa, estado, presupuesto_total)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (id_contrato) DO UPDATE SET
                                bolsa = EXCLUDED.bolsa,
                                estado = EXCLUDED.estado,
                                presupuesto_total = EXCLUDED.presupuesto_total;
                        """, (
                            str(fila.get('id_contrato', '')).strip(),
                            str(fila.get('bolsa', '')).strip(),
                            str(fila.get('estado', 'ACTIVO')).strip(),
                            fila.get('presupuesto_total', 0)
                        ))
                        conn.commit()
                        ok_c += 1
                    except Exception as e:
                        conn.rollback()
                        console.print(f"[yellow] ⚠️ Fila ignorada en Contratos: {e}[/yellow]")
                console.print(f"[green] ✅ Contratos sincronizados: {ok_c}/{len(df_c)}[/green]")

                # --- B. SINCRONIZAR VEHÍCULOS ---
                console.print("Sincronizando [bold]Dim_Vehiculos[/bold]...")
                df_v = pd.DataFrame(hoja.worksheet("Dim_Vehiculos").get_all_records())
                ok_v = 0
                for _, fila in df_v.iterrows():
                    try:
                        cursor.execute("""
                            INSERT INTO dim_vehiculos (placa, tipo_vehiculo, estado, bolsa)
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (placa) DO UPDATE SET
                                tipo_vehiculo = EXCLUDED.tipo_vehiculo,
                                estado = EXCLUDED.estado,
                                bolsa = EXCLUDED.bolsa;
                        """, (
                            str(fila.get('placa', '')).strip().upper(),
                            str(fila.get('tipo_vehiculo', '')).strip(),
                            str(fila.get('estado', 'ACTIVO')).strip(),
                            str(fila.get('bolsa', '')).strip()
                        ))
                        conn.commit()
                        ok_v += 1
                    except Exception as e:
                        conn.rollback()
                        console.print(f"[yellow] ⚠️ Fila ignorada en Vehículos: {e}[/yellow]")
                console.print(f"[green] ✅ Vehículos sincronizados: {ok_v}/{len(df_v)}[/green]")

                # --- C. SINCRONIZAR VALES ---
                console.print("Sincronizando [bold]Fact_Vales[/bold]...")
                df_val = pd.DataFrame(hoja.worksheet("Fact_Vales").get_all_records())
                ok_val = 0
                for _, fila in df_val.iterrows():
                    try:
                        # 1. Limpieza estricta de vacíos para evitar colapsos
                        num_fisico = fila.get('num_vale_fisico', 0)
                        if num_fisico == '': num_fisico = 0
                        
                        gals = fila.get('galones_autorizados', 0.0)
                        if gals == '': gals = 0.0

                        # 2. Traductor Inteligente de Fechas (DD/MM/YYYY -> YYYY-MM-DD)
                        fecha_cruda = str(fila.get('fecha_emision', '')).strip()
                        fecha_sql = None
                        if fecha_cruda:
                            try:
                                # Intenta formatear de latino a estándar ISO
                                fecha_obj = datetime.strptime(fecha_cruda, '%d/%m/%Y')
                                fecha_sql = fecha_obj.strftime('%Y-%m-%d')
                            except ValueError:
                                # Si ya tiene otro formato, se manda cruda
                                fecha_sql = fecha_cruda

                        # 3. Mapeo exacto a las columnas de tu BD
                        cursor.execute("""
                            INSERT INTO fact_vales (id_vale, num_vale_fisico, fecha_emision, placa, id_contrato, galones_autorizados)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id_vale) DO UPDATE SET
                                num_vale_fisico = EXCLUDED.num_vale_fisico,
                                fecha_emision = EXCLUDED.fecha_emision,
                                placa = EXCLUDED.placa,
                                id_contrato = EXCLUDED.id_contrato,
                                galones_autorizados = EXCLUDED.galones_autorizados;
                        """, (
                            str(fila.get('id_vale', str(uuid.uuid4())[:8])).strip(),
                            num_fisico,
                            fecha_sql,
                            str(fila.get('placa', '')).strip().upper(),
                            str(fila.get('id_contrato', '')).strip(),
                            float(gals)
                        ))
                        conn.commit()
                        ok_val += 1
                    except Exception as e:
                        conn.rollback()
                        console.print(f"[yellow] ⚠️ Vale ignorado: {e}[/yellow]")
                console.print(f"[green] ✅ Vales procesados: {ok_val}/{len(df_val)}[/green]")

        console.print("\n[bold green]💾 Sincronización exitosa. Base de datos Cloud actualizada.[/bold green]")

    except Exception as e:
        console.print(f"\n[bold red]❌ ERROR CRÍTICO DE CONEXIÓN: {e}[/bold red]")

if __name__ == "__main__":
    ejecutar_sincronizacion()