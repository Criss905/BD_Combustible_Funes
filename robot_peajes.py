import os
import sys
import glob
import pandas as pd
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ==========================================
# 1. CONFIGURACIÓN CLOUD NATIVE Y CONSTANTES
# ==========================================
load_dotenv()
console = Console()

DB_CONFIG = {
    'dbname': os.getenv('DB_NAME'),
    'user': os.getenv('DB_USER'),
    'password': os.getenv('DB_PASS'),
    'host': os.getenv('DB_HOST'),
    'port': os.getenv('DB_PORT')
}

def encontrar_archivos_facilpass():
    """Busca automáticamente archivos Excel de FacilPass en la carpeta del proyecto o en Descargas."""
    patrones = [
        "FACILPASS*.xlsx",
        "facilpass*.xlsx",
        os.path.expanduser("~/Downloads/FACILPASS*.xlsx")
    ]
    archivos = []
    for patron in patrones:
        archivos.extend(glob.glob(patron))
    # Eliminar duplicados manteniendo el orden y ordenar por fecha de modificación (el más reciente primero)
    archivos_unicos = sorted(list(set(archivos)), key=os.path.getmtime, reverse=True)
    return archivos_unicos

def limpiar_dataframe_facilpass(ruta_excel):
    """Lee el Excel de FacilPass, salta las 13 filas de encabezado y aplica Data Wrangling."""
    # 1. Lectura saltando las primeras 13 filas de metadatos
    df = pd.read_excel(ruta_excel, sheet_name=0, skiprows=13)
    
    # Limpiar posibles espacios en los nombres de las columnas
    df.columns = df.columns.str.strip()
    
    # 2. Filtrar exclusivamente los pasos reales de peaje (descarta 'Recargas' y filas vacías)
    df_peajes = df[df['Detalle'].astype(str).str.strip() == 'Paso Peaje Prepago'].copy()
    df_peajes = df_peajes.dropna(subset=['Id de Transacción', 'Placa', 'Fecha y Hora de Consumo'])
    
    # 3. Estandarización de tipos y formatos
    df_peajes['id_transaccion'] = df_peajes['Id de Transacción'].astype(str).str.strip()
    df_peajes['placa'] = df_peajes['Placa'].astype(str).str.strip().str.upper()
    
    # Conversión de fecha y derivación de la llave temporal (date_key = YYYYMMDD)
    df_peajes['dt_cruce'] = pd.to_datetime(df_peajes['Fecha y Hora de Consumo'], errors='coerce')
    df_peajes = df_peajes.dropna(subset=['dt_cruce'])
    
    df_peajes['date_key'] = df_peajes['dt_cruce'].dt.strftime('%Y%m%d').astype(int)
    df_peajes['fecha_hora_cruce'] = df_peajes['dt_cruce'].dt.strftime('%Y-%m-%d %H:%M:%S')
    
    # Campos descriptivos y valor en positivo (absoluto)
    df_peajes['punto_servicio'] = df_peajes['Punto de Servicio'].fillna('EL_PLACER').astype(str).str.strip()
    df_peajes['sentido'] = df_peajes['Sentido'].fillna('NO_ESPECIFICADO').astype(str).str.strip()
    df_peajes['categoria_cobrada'] = df_peajes['Categoría Cobrada'].fillna('N/A').astype(str).str.strip()
    df_peajes['valor_cobrado'] = pd.to_numeric(df_peajes['Valor'], errors='coerce').fillna(0).abs()
    
    return df_peajes

def ejecutar_ingesta_peajes():
    console.print(Panel.fit("[bold blue]🛣️ ROBOT DE PEAJES (FACILPASS -> SUPABASE)[/bold blue]", subtitle="Data Wrangling + Bulk Upsert Idempotente"))

    if not all([os.getenv('DB_HOST'), os.getenv('DB_PASS')]):
        console.print("[bold red]❌ Error: Faltan credenciales de base de datos en el archivo .env[/bold red]")
        sys.exit(1)

    archivos = encontrar_archivos_facilpass()
    if not archivos:
        console.print("[yellow]📭 No se encontró ningún archivo 'FACILPASS*.xlsx' en el proyecto ni en Descargas. Omitiendo fase.[/yellow]")
        return

    ruta_objetivo = archivos[0]
    console.print(f"[cyan]📄 Procesando archivo más reciente:[/cyan] [bold]{os.path.basename(ruta_objetivo)}[/bold]")

    try:
        df_limpio = limpiar_dataframe_facilpass(ruta_objetivo)
        
        if df_limpio.empty:
            console.print("[yellow]⚠️ El archivo no contiene registros válidos de 'Paso Peaje Prepago'.[/yellow]")
            return

        with psycopg2.connect(**DB_CONFIG) as conn:
            with conn.cursor() as cursor:
                # 1. Verificar placas contra dim_vehiculos para proteger la Integridad Referencial
                cursor.execute("SELECT placa FROM dim_vehiculos WHERE placa IS NOT NULL;")
                placas_registradas = {row[0].strip().upper() for row in cursor.fetchall()}
                
                placas_excel = set(df_limpio['placa'].unique())
                placas_faltantes = placas_excel - placas_registradas
                
                if placas_faltantes:
                    console.print(f"[bold yellow]⚠️ Atención: Placas en FacilPass no encontradas en dim_vehiculos: {placas_faltantes}[/bold yellow]")
                    console.print("[yellow]Filtrando únicamente vehículos registrados para evitar fallo de llave foránea...[/yellow]")
                    df_limpio = df_limpio[df_limpio['placa'].isin(placas_registradas)]

                # 2. Preparar tuplas para inserción en bloque (Bulk Upsert)
                valores_peajes = [
                    (
                        row['id_transaccion'],
                        row['placa'],
                        int(row['date_key']),
                        row['fecha_hora_cruce'],
                        row['punto_servicio'],
                        row['sentido'],
                        row['categoria_cobrada'],
                        float(row['valor_cobrado'])
                    )
                    for _, row in df_limpio.iterrows()
                ]

                query_upsert = """
                    INSERT INTO fact_peajes (
                        id_transaccion, placa, date_key, fecha_hora_cruce,
                        punto_servicio, sentido, categoria_cobrada, valor_cobrado
                    )
                    VALUES %s
                    ON CONFLICT (id_transaccion) DO UPDATE SET
                        placa = EXCLUDED.placa,
                        date_key = EXCLUDED.date_key,
                        fecha_hora_cruce = EXCLUDED.fecha_hora_cruce,
                        punto_servicio = EXCLUDED.punto_servicio,
                        sentido = EXCLUDED.sentido,
                        categoria_cobrada = EXCLUDED.categoria_cobrada,
                        valor_cobrado = EXCLUDED.valor_cobrado;
                """

                psycopg2.extras.execute_values(cursor, query_upsert, valores_peajes)
                conn.commit()

                # 3. Mostrar tabla resumen de auditoría en consola
                resumen = df_limpio.groupby('placa').agg(
                    cruces=('id_transaccion', 'count'),
                    total=('valor_cobrado', 'sum')
                ).sort_values(by='total', ascending=False).reset_index()

                tabla = Table(title="Resumen de Peajes Sincronizados en Supabase")
                tabla.add_column("Placa", style="cyan", justify="center")
                tabla.add_column("Cruces", style="bold white", justify="right")
                tabla.add_column("Total Cobrado", style="green", justify="right")

                for _, r in resumen.iterrows():
                    tabla.add_row(r['placa'], str(r['cruces']), f"${r['total']:,.0f}")

                console.print(tabla)
                console.print(f"\n[bold green]✅ Peajes sincronizados con éxito: {len(valores_peajes)} transacciones aseguradas en 'fact_peajes'.[/bold green]")

    except Exception as e:
        console.print(f"\n[bold red]❌ Error durante la sincronización de peajes: {e}[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    ejecutar_ingesta_peajes()