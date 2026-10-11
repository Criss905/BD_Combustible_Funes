import subprocess
from rich.console import Console
from rich.panel import Panel

console = Console()

def ejecutar_auditoria_completa():
    console.print(Panel.fit("[bold yellow]👑 ROBOT MAESTRO V8.0 - ORQUESTADOR CLOUD[/bold yellow]", subtitle="Alcaldía de Funes | Motor de Conciliación 1:1"))
    
    try:
        # =================================================================================
        # FASE 1: CARGA DE DIMENSIONES Y EVENTOS ORIGEN (Requisito de Integridad Referencial)
        # =================================================================================
        # Esto DEBE ejecutarse primero para poblar las llaves foráneas necesarias en la base 
        # de datos (id_contrato, id_vale, placa, id_personal) antes de intentar cobrar algo.
        console.print("\n[bold cyan]Iniciando Fase 1: Sincronización de Catálogos, Personal y Vales...[/bold cyan]")
        subprocess.run(["python", "sincronizar_drive_sql.py"], check=True)
        
        # =================================================================================
        # FASE 2: EXTRACCIÓN Y CONCILIACIÓN DE FACTURAS DIAN
        # =================================================================================
        # Ahora que los vales y contratos existen en Supabase, el robot de facturas puede 
        # hacer el "matching" 1:1 y enlazar correctamente el gasto.
        console.print("\n[bold cyan]Iniciando Fase 2: Extracción de Facturación DIAN y Conciliación...[/bold cyan]")
        subprocess.run(["python", "robot_facturas.py"], check=True)
        
        console.print("\n[bold green]✅ AUDITORÍA GENERAL COMPLETADA CON ÉXITO. Matriz financiera sincronizada en PostgreSQL.[/bold green]")
        
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]⚠️ Se detectó un error crítico en uno de los módulos. El orquestador detuvo el flujo por seguridad. Código de error: {e.returncode}[/bold red]")
    except Exception as e:
        console.print(f"\n[bold red]❌ Error fatal del Orquestador: {e}[/bold red]")

if __name__ == "__main__":
    ejecutar_auditoria_completa()