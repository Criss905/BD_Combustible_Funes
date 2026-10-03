import subprocess
from rich.console import Console
from rich.panel import Panel

console = Console()

def ejecutar_auditoria_completa():
    console.print(Panel.fit("[bold yellow]👑 ROBOT MAESTRO V7.0 - ORQUESTADOR CLOUD[/bold yellow]", subtitle="Alcaldía de Funes"))
    
    try:
        # PASO 1: Descargar Facturas XML de la DIAN (Estación El Placer)
        console.print("\n[bold cyan]Iniciando Fase 1: Extracción de Facturación DIAN...[/bold cyan]")
        subprocess.run(["python", "robot_facturas.py"], check=True)
        
        # PASO 2: Sincronizar AppSheet / Google Sheets con PostgreSQL
        console.print("\n[bold cyan]Iniciando Fase 2: Sincronización de Planeación y Vales...[/bold cyan]")
        subprocess.run(["python", "sincronizar_drive_sql.py"], check=True)
        
        console.print("\n[bold green]✅ AUDITORÍA GENERAL COMPLETADA CON ÉXITO. PostgreSQL en la nube actualizado.[/bold green]")
        
    except subprocess.CalledProcessError as e:
        console.print(f"\n[bold red]⚠️ Se detectó un error en uno de los módulos. El proceso se detuvo por seguridad. Código de error: {e.returncode}[/bold red]")
    except Exception as e:
        console.print(f"\n[bold red]❌ Error crítico del Orquestador: {e}[/bold red]")

if __name__ == "__main__":
    ejecutar_auditoria_completa()