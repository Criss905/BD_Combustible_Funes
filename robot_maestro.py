import os
import sys
import subprocess
from rich.console import Console
from rich.panel import Panel

console = Console()

def ejecutar_auditoria_completa():
    console.print(
        Panel.fit(
            "[bold yellow]👑 ROBOT MAESTRO V9.0 - ORQUESTADOR CLOUD INTEGRAL[/bold yellow]",
            subtitle="Alcaldía de Funes | Drive + Facturación DIAN + Peajes FacilPass"
        )
    )
    
    try:
        # =================================================================================
        # FASE 1: CARGA DE DIMENSIONES, VALES Y BITÁCORA (Requisito de Integridad Referencial)
        # =================================================================================
        # Se ejecuta primero para poblar las llaves foráneas necesarias en Supabase
        # (id_contrato, id_vale, placa, id_personal) antes de registrar cobros o peajes.
        console.print("\n[bold cyan]Iniciando Fase 1: Sincronización de Catálogos, Personal, Vales y Bitácora...[/bold cyan]")
        subprocess.run([sys.executable, "sincronizar_drive_sql.py"], check=True)
        
        # =================================================================================
        # FASE 2: EXTRACCIÓN Y CONCILIACIÓN DE FACTURAS DIAN (1:1 Vales vs Facturas)
        # =================================================================================
        # Con los vales y contratos actualizados en Supabase, el robot de facturas
        # realiza el emparejamiento 1:1 y enlaza el gasto presupuestal.
        console.print("\n[bold cyan]Iniciando Fase 2: Extracción de Facturación DIAN y Conciliación...[/bold cyan]")
        subprocess.run([sys.executable, "robot_facturas.py"], check=True)

        # =================================================================================
        # FASE 3: INGESTA Y AUDITORÍA DE PEAJES (FACILPASS -> SUPABASE)
        # =================================================================================
        # Procesa automáticamente el reporte Excel más reciente de FacilPass si está presente
        # y realiza un Bulk Upsert idempotente en la tabla fact_peajes.
        if os.path.exists("robot_peajes.py"):
            console.print("\n[bold cyan]Iniciando Fase 3: Sincronización de Cruces de Peaje (FacilPass)...[/bold cyan]")
            subprocess.run([sys.executable, "robot_peajes.py"], check=True)
        else:
            console.print("\n[yellow]⚠️ Omitiendo Fase 3: No se encontró el archivo 'robot_peajes.py' en el directorio.[/yellow]")
        
        console.print("\n[bold green]✅ AUDITORÍA GENERAL COMPLETADA CON ÉXITO. Matriz financiera y operativa sincronizada en PostgreSQL.[/bold green]")
        
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]⚠️ Se detectó un error crítico en uno de los módulos. El orquestador detuvo el flujo por seguridad. Código de error: {e.returncode}[/bold red]")
        sys.exit(e.returncode)
    except Exception as e:
        console.print(f"\n[bold red]❌ Error fatal del Orquestador: {e}[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    ejecutar_auditoria_completa()