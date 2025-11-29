import platform
import sys
import traceback
import subprocess
import atexit
import concurrent
import os
import posixpath
import queue
import socket
import sqlite3
import shutil
import time
import threading
import functools
import plistlib
import zipfile
from pathlib import Path, PurePosixPath
from threading import Timer
from http.server import HTTPServer, SimpleHTTPRequestHandler

import asyncio
import click
import requests
from packaging.version import parse as parse_version
from pymobiledevice3.cli.cli_common import Command
from pymobiledevice3.exceptions import NoDeviceConnectedError, PyMobileDevice3Exception, DeviceNotFoundError
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.lockdown_service_provider import LockdownServiceProvider
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.services.dvt.dvt_secure_socket_proxy import DvtSecureSocketProxyService
from pymobiledevice3.tunneld.api import async_get_tunneld_devices
from pymobiledevice3.services.os_trace import OsTraceService
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.services.dvt.instruments.process_control import ProcessControl

def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()

def start_http_server():
    handler = functools.partial(SimpleHTTPRequestHandler)
    httpd = HTTPServer(("0.0.0.0", 0), handler)
    click.secho(f"Starting HTTP server on port {httpd.server_port}", fg="bright_black")
    global http_server_info
    http_server_info = (get_lan_ip(), httpd.server_port)
    httpd.serve_forever()
    

def main_callback(service_provider: LockdownClient, dvt: DvtSecureSocketProxyService):
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()
    while http_server_info == (None, None):
        time.sleep(0.1)
    ip, port = http_server_info
    click.secho(f"Hosting temporary http server on: http://{ip}:{port}/", fg="bright_black")

    afc = AfcService(lockdown=service_provider)
    pc = ProcessControl(dvt)
    
    # Find bookassetd container UUID
    uuid_file = Path("uuid.txt")
    uuid = uuid_file.read_text().strip() if uuid_file.exists() else ""
    if len(uuid) < 10:
        try:
            pc.launch("com.apple.iBooks")
        except Exception as e:
            click.secho(f"Error launching Books app: {e}", fg="red")
            return
        click.secho("Finding bookassetd container UUID...", fg="yellow")
        click.secho("Please open Books app and download a book to continue.", fg="yellow")
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            if (PurePosixPath(syslog_entry.filename).name != 'bookassetd') or \
                    not "/Documents/BLDownloads/" in syslog_entry.message:
                continue
            uuid = syslog_entry.message.split("/var/containers/Shared/SystemGroup/")[1] \
                    .split("/Documents/BLDownloads")[0]
            click.secho(f"Found bookassetd container UUID: {uuid}", fg="yellow")
            uuid_file.write_text(uuid)
            break
    else:
        click.secho("Saved bookassetd container UUID: " + uuid, fg="green")
    
    
    bldb_server_prefix = f"http://{ip}:{port}/tmp.BLDatabaseManager.sqlite"

    # Kill bookassetd and Books processes to stop them from updating BLDatabaseManager.sqlite
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid_bookassetd = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
    pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
    if pid_bookassetd:
        click.secho(f"Stopping bookassetd pid {pid_bookassetd}...", fg="yellow")
        pc.signal(pid_bookassetd, 19)
    if pid_books:
        click.secho(f"Killing Books pid {pid_books}...", fg="yellow")
        pc.kill(pid_books)
    
    total_files = 1
    relative_files = []
    if overridefile.is_dir():
        # Upload directory
        total_files = 0
        click.secho(f"Uploading contents of directory '{overridefile}'", fg="yellow")
        for root, dirs, files in os.walk(overridefile):
            for file in files:
                local_file_path = Path(root) / file
                relative_path = local_file_path.relative_to(overridefile)
                relative_files.append(relative_path)
                remote_file_path = f"Downloads/{relative_path.as_posix()}"
                # remote_file_paths.append(remote_file_path)
                click.secho(f"Checking {relative_path.as_posix()} -> {remote_file_path}", fg="bright_black")
                total_files += 1
        # We use a HTTP server for this to work properly, so AFC upload is useless here.
    else:
        # Upload the file
        click.secho(f"Checking {overridefile.name}", fg="yellow")
        remote_file_path = f"Downloads/{path.name}"
        relative_files.append(Path(path.name))

    # WIP iOS slop
    shutil.rmtree("Downloads", ignore_errors=True)
    Path("Downloads").mkdir()
    if overridefile.is_file():
        shutil.copyfile(overridefile, Path("Downloads") / overridefile.name)
    else:
        shutil.copytree(overridefile, Path("Downloads"), dirs_exist_ok=True)

    # Upload iTunesMetadata.plist
    click.secho("Uploading iTunesMetadata.plist...", fg="yellow")
    afc.push("iTunesMetadata.plist", "Books/iTunesMetadata.plist")

    # Loop so that we can download multiple files if needed
    for (i, relative_path) in enumerate(relative_files):
        click.secho(f"Processing file {i+1} of {total_files}: {relative_path.as_posix()}", fg="yellow")

        # Modify BLDatabaseManager.sqlite
        # Copy BLDatabaseManager.sqlite to tmp.BLDatabaseManager.sqlite
        if total_files == 1:
            filetooverwritename = str(path)
        else:
            filetooverwritename = str(path.joinpath(relative_path))
        click.secho(f"File to overwrite on device: {filetooverwritename}", fg="bright_black")
        click.secho("Relative path: " + str(relative_path), fg="bright_black")

        # Craft our epub file here
        epub_path = Path("hax.epub")
        target_file = overridefile.joinpath(relative_path)
        if total_files == 1:
            target_file = overridefile
        with zipfile.ZipFile(epub_path, 'w') as epub:
            # Add mimetype file
            epub.writestr("Caches/mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
            epub.write(target_file, f"Caches/{relative_path.as_posix()}", compress_type=zipfile.ZIP_DEFLATED)
        
        # Upload our crafted epub
        afc.push(epub_path, "Books/asset.epub")
        shutil.copyfile("BLDatabaseManager.sqlite", "tmp.BLDatabaseManager.sqlite")
        blconn = sqlite3.connect("tmp.BLDatabaseManager.sqlite")
        cursor = blconn.cursor()

        # What the fuck I'm die trying...
        sequel_command = f"""
        UPDATE ZBLDOWNLOADINFO
        SET 
            ZASSETPATH = '/private/var/mobile/Media/Books/asset.epub',
            ZDOWNLOADID = '../../../../../../{filetooverwritename}',
            ZPLISTPATH = '/var/mobile/Media/Books/iTunesMetadata.plist',
            ZURL = 'http://{ip}:{port}/Downloads/{relative_path.as_posix()}'
        """
        click.secho(sequel_command, fg="bright_black")
        cursor.execute(sequel_command)
        blconn.commit()

        # Modify downloads.28.sqlitedb
        # Copy downloads.28.sqlitedb to tmp.downloads.28.sqlitedb
        shutil.copyfile("downloads.28.sqlitedb", "tmp.downloads.28.sqlitedb")
        conn = sqlite3.connect("tmp.downloads.28.sqlitedb")
        cursor = conn.cursor()
        bldb_local_prefix = f"/private/var/containers/Shared/SystemGroup/{uuid}/Documents/BLDatabaseManager/BLDatabaseManager.sqlite"
        cursor.execute(f"""
        UPDATE asset
        SET local_path = CASE
            WHEN local_path LIKE '%/BLDatabaseManager.sqlite'
                THEN '{bldb_local_prefix}'
            WHEN local_path LIKE '%/BLDatabaseManager.sqlite-shm'
                THEN '{bldb_local_prefix}-shm'
            WHEN local_path LIKE '%/BLDatabaseManager.sqlite-wal'
                THEN '{bldb_local_prefix}-wal'
        END
        WHERE local_path LIKE '/private/var/containers/Shared/SystemGroup/%/Documents/BLDatabaseManager/BLDatabaseManager.sqlite%'
        """)
        cursor.execute(f"""
        UPDATE asset
        SET url = CASE
            WHEN url LIKE '%/BLDatabaseManager.sqlite'
                THEN '{bldb_server_prefix}'
            WHEN url LIKE '%/BLDatabaseManager.sqlite-shm'
                THEN '{bldb_server_prefix}-shm'
            WHEN url LIKE '%/BLDatabaseManager.sqlite-wal'
                THEN '{bldb_server_prefix}-wal'
        END
        WHERE url LIKE '%/BLDatabaseManager.sqlite%'
        """)
        conn.commit()

        # Upload downloads.28.sqlitedb
        click.secho(f"Uploading downloads.28.sqlitedb for file {i+1} of {total_files}", fg="yellow")
        afc.push("tmp.downloads.28.sqlitedb", "Downloads/downloads.28.sqlitedb")
        afc.push("tmp.downloads.28.sqlitedb-shm", "Downloads/downloads.28.sqlitedb-shm")
        afc.push("tmp.downloads.28.sqlitedb-wal", "Downloads/downloads.28.sqlitedb-wal")
        
        # Kill itunesstored to trigger BLDataBaseManager.sqlite overwrite
        procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
        pid_itunesstored = next((pid for pid, p in procs.items() if p['ProcessName'] == 'itunesstored'), None)
        if pid_itunesstored:
            click.secho(f"Killing itunesstored pid {pid_itunesstored}...", fg="yellow")
            pc.kill(pid_itunesstored)
        
        # Wait for itunesstored to finish download and raise an error
        click.secho("Waiting for itunesstored to finish download...", fg="yellow")
        download_timeout = 30  # seconds
        download_start_time = time.time()
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            # Check for timeout
            if time.time() - download_start_time > download_timeout:
                click.secho("Download wait timeout reached, continuing...", fg="red")
                break
            
            if "6936249076851270150" in syslog_entry.message:
                click.secho(f"Found syslog entry: {syslog_entry.message}", fg="bright_black")
            
            # Check for various completion states
            if "Install complete for download: 6936249076851270150" in syslog_entry.message:
                click.secho(f"Download complete: {syslog_entry.message}", fg="bright_black")
                break
            # Check for download finished (success or failure)
            if "6936249076851270150" in syslog_entry.message and ("finished" in syslog_entry.message.lower() or "complete" in syslog_entry.message.lower() or "failed" in syslog_entry.message.lower() or "error" in syslog_entry.message.lower()):
                click.secho(f"Download state changed: {syslog_entry.message}", fg="bright_black")
                break
            # Check if download was added to asset queue (means it's processing)
            if "AssetDownloadDelegate" in syslog_entry.message and "6936249076851270150" in syslog_entry.message:
                click.secho(f"Asset delegate: {syslog_entry.message}", fg="bright_black")
            # Check for BLDatabaseManager being written
            if "BLDatabaseManager" in syslog_entry.message:
                click.secho(f"BLDatabaseManager activity: {syslog_entry.message}", fg="bright_black")

        # Kill bookassetd and Books processes to trigger file overwrite
        pid_bookassetd = next((pid for pid, p in procs.items() if p['ProcessName'] == 'bookassetd'), None)
        pid_books = next((pid for pid, p in procs.items() if p['ProcessName'] == 'Books'), None)
        if pid_bookassetd:
            click.secho(f"Killing bookassetd pid {pid_bookassetd}...", fg="yellow")
            pc.kill(pid_bookassetd)
        if pid_books:
            click.secho(f"Killing Books pid {pid_books}...", fg="yellow")
            pc.kill(pid_books)
        
        # Re-open Books app
        try:
            click.secho(f"Started Books with pid {pc.launch("com.apple.iBooks")}", fg="yellow")
        except Exception as e:
            click.secho(f"Error launching Books app: {e}", fg="red")
            return
        
        click.secho("If this takes more than a minute please try again.", fg="yellow")
        click.secho("Waiting for file overwrite to complete...", fg="yellow")
        success_message = ") [Install-Mgr]: Marking download as [finished]"
        cancelled_message = ") [Install-Mgr]: Marking download as [cancelled]"
        for syslog_entry in OsTraceService(lockdown=service_provider).syslog():
            # click.secho(f"Syslog: {syslog_entry.message}", fg="bright_black")
            if "Install-Mgr" in syslog_entry.message:
                click.secho(f"Found Install-Mgr message: {syslog_entry.message}", fg="bright_black")
            if success_message in syslog_entry.message:
                click.secho(f"Found install-mgr success message: {syslog_entry.message}", fg="bright_black")
            if "hax.epub" in syslog_entry.message:
                click.secho(f"Found hax.epub: {syslog_entry.message}", fg="bright_black")
            if (PurePosixPath(syslog_entry.filename).name == 'bookassetd') and \
                    success_message in syslog_entry.message and relative_path.name in syslog_entry.message:
                    break
            if (PurePosixPath(syslog_entry.filename).name == 'bookassetd') and \
                    cancelled_message in syslog_entry.message and relative_path.name in syslog_entry.message:
                    click.secho("Error: File overwrite was cancelled.", fg="red")
                    break
        pc.kill(pid_bookassetd)
    click.secho("Overwrite successful! Respringing...", fg="green")
    procs = OsTraceService(lockdown=service_provider).get_pid_list().get("Payload")
    pid = next((pid for pid, p in procs.items() if p['ProcessName'] == 'backboardd'), None)
    pc.kill(pid)
    click.secho("Done!", fg="green")

    blconn.close()
    conn.close()
    
    sys.exit(0)

async def _run_async_rsd_connection(address, port):
    try:
        async def async_connection():
            async with RemoteServiceDiscoveryService((address, port)) as rsd:
                click.secho("connected to tunnel", fg="green")
                loop = asyncio.get_running_loop()
                    
                def run_blocking_callback():
                    with DvtSecureSocketProxyService(rsd) as dvt:
                        main_callback(rsd, dvt)
                    
                await loop.run_in_executor(None, run_blocking_callback)

        click.secho(f"attempt connection", fg="bright_black")
        await async_connection()

        return

    except (ConnectionRefusedError, OSError) as e:
        click.secho(f"tunnel connect failed: {e}", fg="red")
        raise

def exit_func(tunnel_proc):
    tunnel_proc.terminate()

async def create_tunnel(udid):
    command = [
        sys.executable,
        "-m", "pymobiledevice3",
        "lockdown", "start-tunnel",
        "--script-mode",
        "--udid", udid
    ]
    tunnel_process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    atexit.register(exit_func, tunnel_process)
    while True:
        output = tunnel_process.stdout.readline()
        if output:
            rsd_val = output.decode().strip()
            break
        if tunnel_process.poll() is not None:
            error = tunnel_process.stderr.readlines()
            if error:
                not_connected = None
                admin_error = None
                for i in range(len(error)):
                    if (error[i].find(b'connected') > -1):
                        not_connected = True
                    if (error[i].find(b'admin') > -1):
                        admin_error = True
                if not_connected:
                    print("It seems like your device isn't connected.", error)
                elif admin_error:
                    print("It seems like you're not running this script as admin, which is required.", error)
                else:
                    print("Error opening a tunnel.", error)
                sys.exit()
            break
    rsd_str = str(rsd_val)
    click.secho("Sucessfully created tunnel: " + rsd_str, fg="green")
    click.secho(f"address: {rsd_str.split(" ")[0]}", fg="bright_black")
    port = int(rsd_str.split(" ")[1])
    click.secho(f"port: {port}", fg="bright_black")
    time.sleep(2)
    return {"address": rsd_str.split(" ")[0], "port": int(rsd_str.split(" ")[1])}

async def connection_context(service_provider):# Create a LockdownClient instance
    try:
        marketing_name = service_provider.get_value(key="MarketingName")
        marketing_name = service_provider.get_value(key="MarketingName")
        device_build = service_provider.get_value(key="BuildVersion")
        device_product_type = service_provider.get_value(key="ProductType")
        device_version = parse_version(service_provider.product_version)
        click.secho(f"Got device: {marketing_name} (iOS {device_version}, Build {device_build})", fg="blue")
        click.secho("Please keep your device unlocked during the process.", fg="blue")
        
        # Validate MobileGestalt file
        if path.name == "com.apple.MobileGestalt.plist":
            mg_contents = plistlib.load(Path(overridefile).open("rb"))
            cache_extra = mg_contents["CacheExtra"]
            if cache_extra is None:
                click.secho("Error: Invalid com.apple.MobileGestalt.plist file", fg="red")
                return
            cache_build_version = mg_contents["CacheVersion"]
            cache_product_type = cache_extra["0+nc/Udy4WNG8S+Q7a/s1A"] # ThinningProductType
            if cache_build_version != device_build or cache_product_type != device_product_type:
                click.secho("Error: It seems you are using MobileGestalt file for a different device", fg="red")
                click.secho(f"Device Build: {device_build}, MobileGestalt Build: {cache_build_version}", fg="red")
                click.secho(f"Device ProductType: {device_product_type}, MobileGestalt ProductType: {cache_product_type}", fg="red")
                # return
        
        if device_version >= parse_version('17.0'):
            available_address = await create_tunnel(service_provider.udid)
            if available_address:
                await _run_async_rsd_connection(available_address["address"], available_address["port"])
            else:
                raise Exception("An error occurred getting tunnels addresses...")
        else:
            # Use USB Mux
            with DvtSecureSocketProxyService(lockdown=service_provider) as dvt:
                main_callback(service_provider, dvt)
    except OSError:  # no route to host (Intel fix)
        pass
    except DeviceNotFoundError:
        click.secho("Device not found. Make sure it's unlocked.", fg="red")
    except Exception as e:
        raise Exception(f"Connection not established... {e}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python run.py /path/to/local/file (ex. ./MobileGestalt/com.apple.MobileGestalt.plist) /path/to/file_on_iOS (like /private/var/containers/Shared/SystemGroup/systemgroup.com.apple.mobilegestaltcache/Library/Caches/com.apple.MobileGestalt.plist)")
        exit(1)
    lockdown = create_using_usbmux()
    http_server_info = (None, None)
    # overridefile is the local file to upload
    overridefile = Path(sys.argv[1])
    # path is the path on the iOS device to overwrite
    path = PurePosixPath(sys.argv[2])
    
    os.chdir(Path(__file__).resolve().parent)
    asyncio.run(connection_context(lockdown))
