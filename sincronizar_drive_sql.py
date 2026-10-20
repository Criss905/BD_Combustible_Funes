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

# ID del documento en Google Sheets (configurable desde variables de entorno)
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
    """Convierte cadenas vacías de Google Sheets a None (NULL en SQL) y corrige codificación UTF-8"""
    if valor is None:
        return None
    val_str = str(valor).strip()
    if val_str == "":
        return None
    try:
        # Fuerza la recodificación de lecturas Latin-1 de vuelta a UTF-8
        val_str = val_str.encode('latin1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return val_str

def limpiar_placa(fila):
    """Extrae la placa, repara la codificación Latin-1 -> UTF-8 y normaliza en mayúsculas"""
    placa_cruda = str(fila.get('placa', '')).strip().upper()
    if not placa_cruda:
        return None
    try:
        # Fuerza la recodificación a UTF-8
        placa = placa_cruda.encode('latin1').decode('utf-8').upper()
    except (UnicodeEncodeError, UnicodeDecodeError):
        # Si la cadena ya estaba sana, la deja igual
        placa = placa_cruda
    return placa

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
                try:
                    records_c = hoja.worksheet("Dim_Contratos").get_all_records()
                    valores_c = []
                    for fila in records_c:
                        id_c = limpiar_string(fila.get('id_contrato'))
                        if id_c:  # Solo procesa si hay un ID válido
                            presupuesto = fila.get('presupuesto_total')
                            # Asegurar que el presupuesto sea un número flotante válido
                            presupuesto = float(presupuesto) if presupuesto not in ["", None] else 0.0
                            valores_c.append((
                                id_c,
                                limpiar_string(fila.get('bolsa')),
                                limpiar_string(fila.get('estado')) or 'ACTIVO',
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
                except Exception as e:
                    conn.rollback()
                    console.print(f"[yellow]⚠️ Omitiendo Contratos o error al leer hoja: {e}[/yellow]")

                # --- B. SINCRONIZAR VEHÍCULOS (BATCH) ---
                console.print("Sincronizando [bold]Dim_Vehiculos[/bold]...")
                try:
                    records_v = hoja.worksheet("Dim_Vehiculos").get_all_records()
                    valores_v = []
                    for fila in records_v:
                        placa = limpiar_placa(fila)
                        if placa:
                            valores_v.append((
                                placa,
                                limpiar_string(fila.get('tipo_vehiculo')),
                                limpiar_string(fila.get('estado')) or 'ACTIVO',
                                limpiar_string(fila.get('bolsa'))
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
                except Exception as e:
                    conn.rollback()
                    console.print(f"[yellow]⚠️ Omitiendo Vehículos o error al leer hoja: {e}[/yellow]")

                # --- C. SINCRONIZAR PERSONAL (BATCH) ---
                console.print("Sincronizando [bold]Dim_Personal[/bold]...")
                try:
                    records_p = hoja.worksheet("Dim_Personal").get_all_records()
                    valores_p = []
                    for fila in records_p:
                        id_p = limpiar_string(fila.get('id_personal'))
                        nombre = limpiar_string(fila.get('nombre'))
                        if id_p and nombre:
                            valores_p.append((
                                id_p,
                                nombre,
                                limpiar_string(fila.get('cargo')),
                                limpiar_string(fila.get('tipo_vinculacion'))
                            ))
                    
                    if valores_p:
                        query_p = """
                            INSERT INTO dim_personal (id_personal, nombre, cargo, tipo_vinculacion)
                            VALUES %s
                            ON CONFLICT (id_personal) DO UPDATE SET
                                nombre = EXCLUDED.nombre,
                                cargo = EXCLUDED.cargo,
                                tipo_vinculacion = EXCLUDED.tipo_vinculacion;
                        """
                        psycopg2.extras.execute_values(cursor, query_p, valores_p)
                        conn.commit()
                        console.print(f"[green] ✅ Personal sincronizado: {len(valores_p)}[/green]")
                except Exception as e:
                    conn.rollback()
                    console.print(f"[yellow]⚠️ Omitiendo Personal o no existe la hoja aún: {e}[/yellow]")

                # --- D. SINCRONIZAR VALES (BATCH) ---
                console.print("Sincronizando [bold]Fact_Vales[/bold]...")
                try:
                    records_val = hoja.worksheet("Fact_Vales").get_all_records()
                    valores_val = []
                    for fila in records_val:
                        placa = limpiar_placa(fila)
                        id_contrato = limpiar_string(fila.get('id_contrato'))
                        
                        # Validación estricta NOT NULL
                        if placa and id_contrato:
                            id_vale = limpiar_string(fila.get('id_vale'))
                            if not id_vale:
                                id_vale = str(uuid.uuid4())[:8]
                                
                            num_fisico = fila.get('num_vale_fisico')
                            num_fisico = int(num_fisico) if num_fisico not in ["", None] else None
                            
                            gals = fila.get('galones_autorizados')
                            gals = float(gals) if gals not in ["", None] else 0.0
                            
                            fecha_iso = formatear_fecha(fila.get('fecha_emision'))
                            # === DATA WRANGLING: Llave Temporal (date_key) a prueba de texto ===
                            try:
                                date_key = int(fecha_iso[:10].replace('-', '')) if fecha_iso else None
                            except ValueError:
                                date_key = None  # Convierte textos inválidos en nulo
                            
                            # Omitir si la fecha es nula o inválida, ya que requerimos date_key
                            if date_key is not None:
                                valores_val.append((
                                    id_vale,
                                    num_fisico,
                                    fecha_iso,
                                    date_key,
                                    placa,
                                    id_contrato,
                                    gals
                                ))

                    if valores_val:
                        query_val = """
                            INSERT INTO fact_vales (id_vale, num_vale_fisico, fecha_emision, date_key, placa, id_contrato, galones_autorizados)
                            VALUES %s
                            ON CONFLICT (id_vale) DO UPDATE SET
                                num_vale_fisico = EXCLUDED.num_vale_fisico,
                                fecha_emision = EXCLUDED.fecha_emision,
                                date_key = EXCLUDED.date_key,
                                placa = EXCLUDED.placa,
                                id_contrato = EXCLUDED.id_contrato,
                                galones_autorizados = EXCLUDED.galones_autorizados;
                        """
                        psycopg2.extras.execute_values(cursor, query_val, valores_val)
                        conn.commit()
                        console.print(f"[green] ✅ Vales procesados: {len(valores_val)}[/green]")
                except Exception as e:
                    conn.rollback()
                    console.print(f"[red]❌ Error al procesar Vales: {e}[/red]")
                    
                # --- E. SINCRONIZAR BITÁCORA (BATCH) ---
                console.print("Sincronizando [bold]Fact_Bitacora[/bold]...")
                try:
                    records_b = hoja.worksheet("Fact_Bitacora").get_all_records()
                    valores_b = []
                    for fila in records_b:
                        id_bitacora = limpiar_string(fila.get('id_bitacora'))
                        fecha_cruda = fila.get('fecha_actividad')
                        
                        # Convertimos la lectura corrupta (Latin-1) de vuelta a su formato original (UTF-8)
                        placa_cruda = str(fila.get('placa', '')).strip().upper()
                        try:
                            # Fuerza la recodificación a UTF-8
                            placa = placa_cruda.encode('latin1').decode('utf-8').upper()
                        except (UnicodeEncodeError, UnicodeDecodeError):
                            # Si la cadena ya estaba sana, la deja igual
                            placa = placa_cruda
                        
                        # Exigir placa NOT NULL y fecha válida
                        if id_bitacora and placa and fecha_cruda:
                            fecha_iso = formatear_fecha(fecha_cruda)
                            # === DATA WRANGLING: Llave Temporal (date_key) a prueba de texto ===
                            try:
                                date_key = int(fecha_iso[:10].replace('-', '')) if fecha_iso else None
                            except ValueError:
                                date_key = None
                            
                            if date_key is not None:
                                valores_b.append((
                                    id_bitacora,
                                    placa,
                                    fecha_iso,
                                    date_key,
                                    limpiar_string(fila.get('lugar_recorrido')),
                                    limpiar_string(fila.get('actividad_desarrollada')),
                                    limpiar_string(fila.get('id_conductor')),
                                    limpiar_string(fila.get('id_responsable_1')),
                                    limpiar_string(fila.get('id_responsable_2'))
                                ))

                    if valores_b:
                        query_b = """
                            INSERT INTO fact_bitacora (
                                id_bitacora, placa, fecha_actividad, date_key, 
                                lugar_recorrido, actividad_desarrollada, 
                                id_conductor, id_responsable_1, id_responsable_2
                            )
                            VALUES %s
                            ON CONFLICT (id_bitacora) DO UPDATE SET
                                placa = EXCLUDED.placa,
                                fecha_actividad = EXCLUDED.fecha_actividad,
                                date_key = EXCLUDED.date_key,
                                lugar_recorrido = EXCLUDED.lugar_recorrido,
                                actividad_desarrollada = EXCLUDED.actividad_desarrollada,
                                id_conductor = EXCLUDED.id_conductor,
                                id_responsable_1 = EXCLUDED.id_responsable_1,
                                id_responsable_2 = EXCLUDED.id_responsable_2;
                        """
                        psycopg2.extras.execute_values(cursor, query_b, valores_b)
                        conn.commit()
                        console.print(f"[green] ✅ Bitácora procesada: {len(valores_b)}[/green]")
                except Exception as e:
                    conn.rollback()
                    console.print(f"[yellow]⚠️ Omitiendo Bitácora o no existe la hoja aún: {e}[/yellow]")

        console.print("\n[bold green]💾 Sincronización exitosa. Base de datos Cloud actualizada mediante transacciones en bloque.[/bold green]")

    except Exception as e:
        console.print(f"\n[bold red]❌ ERROR CRÍTICO DE CONEXIÓN O EJECUCIÓN: {e}[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    ejecutar_sincronizacion()