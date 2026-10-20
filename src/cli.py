"""
CLI Module

Command-line interface for the filesystem simulator.
Supports both interactive shell and single-command modes.
"""

import sys
import argparse
import shlex
import textwrap
from typing import List, Optional
from .filesystem import (
    FileSystem, create_filesystem, open_filesystem,
    FileSystemError, PathError, NotFoundError, AlreadyExistsError,
    NotDirectoryError, IsDirectoryError, DirectoryNotEmptyError, DiskFullError
)
from .virtual_disk import CrashInjectedError
from .crash_injector import CrashInjector, CrashMode, CrashPoint, get_global_injector
from .crash_worker import OPERATIONS as LAB_OPERATIONS


class CLI:
    """Command-line interface for the filesystem."""
    
    def __init__(self, fs: FileSystem):
        self.fs = fs
        self.running = True
    
    def run_command(self, args: List[str]) -> int:
        """Run a single command."""
        if not args:
            return 0
        
        cmd = args[0].lower()
        cmd_args = args[1:]
        
        try:
            if cmd in ('help', 'h', '?'):
                self.cmd_help(cmd_args)
            elif cmd in ('format', 'mkfs'):
                self.cmd_format(cmd_args)
            elif cmd in ('mount', 'open'):
                self.cmd_mount(cmd_args)
            elif cmd in ('unmount', 'close'):
                self.cmd_unmount(cmd_args)
            elif cmd in ('mkdir', 'md'):
                self.cmd_mkdir(cmd_args)
            elif cmd in ('create', 'touch'):
                self.cmd_create(cmd_args)
            elif cmd in ('write', 'w'):
                self.cmd_write(cmd_args)
            elif cmd in ('read', 'r', 'cat'):
                self.cmd_read(cmd_args)
            elif cmd in ('append', 'a'):
                self.cmd_append(cmd_args)
            elif cmd in ('ls', 'list', 'dir'):
                self.cmd_ls(cmd_args)
            elif cmd in ('stat', 'st'):
                self.cmd_stat(cmd_args)
            elif cmd in ('rename', 'mv', 'move'):
                self.cmd_rename(cmd_args)
            elif cmd in ('delete', 'rm', 'del'):
                self.cmd_delete(cmd_args)
            elif cmd in ('rmdir', 'rd'):
                self.cmd_rmdir(cmd_args)
            elif cmd in ('df', 'free'):
                self.cmd_df(cmd_args)
            elif cmd in ('dump', 'info'):
                self.cmd_dump(cmd_args)
            elif cmd in ('check', 'fsck'):
                self.cmd_check(cmd_args)
            elif cmd in ('recover', 'recovery'):
                self.cmd_recover(cmd_args)
            elif cmd in ('crash-test', 'crash'):
                self.cmd_crash_test(cmd_args)
            elif cmd in ('enable-crash-injection', 'enable-crash'):
                self.cmd_enable_crash_injection(cmd_args)
            elif cmd in ('disable-crash-injection', 'disable-crash'):
                self.cmd_disable_crash_injection(cmd_args)
            elif cmd in ('journal', 'jdump'):
                self.cmd_journal(cmd_args)
            elif cmd == 'inode':
                self.cmd_inode(cmd_args)
            elif cmd == 'block':
                self.cmd_block(cmd_args)
            elif cmd == 'layout':
                self.cmd_layout(cmd_args)
            elif cmd == 'events':
                self.cmd_events(cmd_args)
            elif cmd == 'checkpoint':
                self.fs.checkpoint()
                print("Checkpoint done")
            elif cmd in ('exit', 'quit', 'q'):
                self.running = False
                return 1  # Signal to exit
            else:
                print(f"Unknown command: {cmd}")
                print("Type 'help' for available commands")
                return 1
        except FileSystemError as e:
            print(f"Error: {e}")
            return 1
        except CrashInjectedError as e:
            print(f"CRASH: {e}  (the simulated process is dead; exit and remount to recover)")
            return 2
        except Exception as e:
            print(f"Unexpected error ({type(e).__name__}): {e}")
            return 1
        
        return 0
    
    def run_interactive(self) -> None:
        """Run interactive shell."""
        print("CRFS Filesystem Simulator")
        print("Type 'help' for commands, 'exit' to quit")
        
        while self.running:
            try:
                line = input("crfs> ").strip()
                if not line:
                    continue
                
                # Parse command line (handle quotes)
                args = shlex.split(line)
                result = self.run_command(args)
                if result == 1 and not self.running:
                    break
            except EOFError:
                break
            except KeyboardInterrupt:
                print("\nInterrupted")
                break
        
        print("Goodbye!")
    
    # Command implementations
    def cmd_help(self, args: List[str]) -> None:
        """Show help."""
        help_text = textwrap.dedent("""
            Available commands:
              format [size] [journal_blocks] [inode_blocks] [label]  - Format new filesystem
              mount                                                  - Mount existing filesystem
              unmount                                                - Unmount filesystem
              mkdir <path>                                           - Create directory
              create <path>                                          - Create empty file
              write <path> <text>                                    - Write text to file
              read <path>                                            - Read file contents
              append <path> <text>                                   - Append text to file
              ls [path]                                              - List directory (default: /)
              stat <path>                                            - Show file/directory info
              rename <old> <new>                                     - Rename/move file or directory
              delete <path>                                          - Delete file
              rmdir <path>                                           - Remove empty directory
              df                                                     - Show disk usage
              dump                                                   - Dump filesystem state
              check                                                  - Run consistency check
              recover                                                - Run crash recovery
              journal                                                - Show transactions found in the journal region
              inode <n> / block <n> / layout                         - Inspect disk structures
              events [n]                                             - Show the last n engine events
              checkpoint                                             - Force a journal checkpoint
              crash-test [scenario] [operation]                      - Run a REAL crash scenario (SIGKILL) on a temp image
              enable-crash-injection <mode>                          - Enable crash injection
              disable-crash-injection                                - Disable crash injection
              help                                                   - Show this help
              exit                                                   - Exit simulator

            Examples:
              format 16M 128 64 "MyVolume"
              mkdir /home/user
              create /home/user/file.txt
              write /home/user/file.txt "Hello World"
              read /home/user/file.txt
              ls /home/user
              stat /home/user/file.txt
              crash-test crash_after_commit_flush_before_apply
            """)
        print(help_text)
    
    def cmd_format(self, args: List[str]) -> None:
        """Format filesystem."""
        if self.fs.is_mounted:
            print("Error: Filesystem already mounted. Unmount first.")
            return
        
        disk_size = 16 * 1024 * 1024
        journal_blocks = 128
        inode_blocks = 64
        label = ""
        
        if args:
            # Parse size (support K, M, G suffixes)
            size_str = args[0]
            if size_str.endswith('K'):
                disk_size = int(size_str[:-1]) * 1024
            elif size_str.endswith('M'):
                disk_size = int(size_str[:-1]) * 1024 * 1024
            elif size_str.endswith('G'):
                disk_size = int(size_str[:-1]) * 1024 * 1024 * 1024
            else:
                disk_size = int(size_str)
        
        if len(args) > 1:
            journal_blocks = int(args[1])
        if len(args) > 2:
            inode_blocks = int(args[2])
        if len(args) > 3:
            label = args[3]
        
        self.fs.format(disk_size=disk_size, journal_blocks=journal_blocks, 
                      inode_blocks=inode_blocks, volume_label=label)
        print(f"Formatted {self.fs.disk_path}: {disk_size} bytes, "
              f"{journal_blocks} journal blocks, {inode_blocks} inode blocks")
    
    def cmd_mount(self, args: List[str]) -> None:
        """Mount filesystem."""
        if self.fs.is_mounted:
            print("Error: Already mounted")
            return
        
        run_recovery = True
        if args and args[0] == '--no-recovery':
            run_recovery = False
        
        self.fs.mount(run_recovery=run_recovery)
        print(f"Mounted {self.fs.disk_path}")
        rec = self.fs.last_recovery
        if run_recovery and rec is not None:
            print(f"Recovery: {rec.transactions_replayed} transaction(s) replayed, "
                  f"{rec.transactions_discarded} discarded")
            for w in rec.warnings:
                print(f"  warning: {w}")
        elif not run_recovery:
            print("Mounted READ-ONLY, recovery not run (use 'recover' to replay the journal)")
    
    def cmd_unmount(self, args: List[str]) -> None:
        """Unmount filesystem."""
        if not self.fs.is_mounted:
            print("Error: Not mounted")
            return
        
        self.fs.unmount()
        print("Unmounted")
    
    def cmd_mkdir(self, args: List[str]) -> None:
        """Create directory."""
        if not args:
            print("Usage: mkdir <path>")
            return
        
        self.fs.mkdir(args[0])
        print(f"Created directory: {args[0]}")
    
    def cmd_create(self, args: List[str]) -> None:
        """Create empty file."""
        if not args:
            print("Usage: create <path>")
            return
        
        self.fs.create(args[0])
        print(f"Created file: {args[0]}")
    
    def cmd_write(self, args: List[str]) -> None:
        """Write to file."""
        if len(args) < 2:
            print("Usage: write <path> <text>")
            return
        
        path = args[0]
        text = ' '.join(args[1:])
        data = text.encode('utf-8')
        
        written = self.fs.write(path, data)
        print(f"Wrote {written} bytes to {path}")
    
    def cmd_read(self, args: List[str]) -> None:
        """Read file."""
        if not args:
            print("Usage: read <path>")
            return
        
        data = self.fs.read(args[0])
        try:
            text = data.decode('utf-8')
            print(text)
        except UnicodeDecodeError:
            print(f"<binary data, {len(data)} bytes>")
            # Hex dump
            for i in range(0, min(len(data), 256), 16):
                hex_part = ' '.join(f'{b:02x}' for b in data[i:i+16])
                ascii_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[i:i+16])
                print(f"{i:08x}  {hex_part:<48}  {ascii_part}")
    
    def cmd_append(self, args: List[str]) -> None:
        """Append to file."""
        if len(args) < 2:
            print("Usage: append <path> <text>")
            return
        
        path = args[0]
        text = ' '.join(args[1:])
        data = text.encode('utf-8')
        
        appended = self.fs.append(path, data)
        print(f"Appended {appended} bytes to {path}")
    
    def cmd_ls(self, args: List[str]) -> None:
        """List directory."""
        path = args[0] if args else "/"
        entries = self.fs.ls(path)
        
        if not entries:
            print(f"{path}: (empty)")
            return
        
        print(f"{'Type':<6} {'Inode':<8} {'Name'}")
        print("-" * 40)
        for entry in entries:
            type_str = "DIR" if entry.file_type == 2 else "FILE"
            print(f"{type_str:<6} {entry.inode_num:<8} {entry.name}")
    
    def cmd_stat(self, args: List[str]) -> None:
        """Show file/directory info."""
        if not args:
            print("Usage: stat <path>")
            return
        
        info = self.fs.stat(args[0])
        print(f"Path: {args[0]}")
        for key, value in info.items():
            print(f"  {key}: {value}")
    
    def cmd_rename(self, args: List[str]) -> None:
        """Rename/move."""
        if len(args) < 2:
            print("Usage: rename <old> <new>")
            return
        
        self.fs.rename(args[0], args[1])
        print(f"Renamed {args[0]} -> {args[1]}")
    
    def cmd_delete(self, args: List[str]) -> None:
        """Delete file."""
        if not args:
            print("Usage: delete <path>")
            return
        
        self.fs.delete(args[0])
        print(f"Deleted: {args[0]}")
    
    def cmd_rmdir(self, args: List[str]) -> None:
        """Remove directory."""
        if not args:
            print("Usage: rmdir <path>")
            return
        
        self.fs.rmdir(args[0])
        print(f"Removed directory: {args[0]}")
    
    def cmd_df(self, args: List[str]) -> None:
        """Show disk usage."""
        info = self.fs.df()
        print(f"Disk usage for {self.fs.disk_path}:")
        print(f"  Total:  {info['total_size']:,} bytes ({info['total_blocks']} blocks)")
        print(f"  Used:   {info['used_size']:,} bytes ({info['used_blocks']} blocks)")
        print(f"  Free:   {info['free_size']:,} bytes ({info['free_blocks']} blocks)")
        print(f"  Inodes: {info['used_inodes']} used, {info['free_inodes']} free of {info['total_inodes']}")
    
    def cmd_dump(self, args: List[str]) -> None:
        """Dump filesystem state."""
        info = self.fs.dump()
        import json
        print(json.dumps(info, indent=2))
    
    def cmd_check(self, args: List[str]) -> None:
        """Run consistency check."""
        rep = self.fs.check_report()
        if rep.passed:
            print("Consistency check PASSED")
        else:
            print(f"Consistency check FAILED ({len(rep.errors)} errors)")
        for f in rep.findings:
            print(f"  [{f['severity'].upper():7}] {f['code']}: {f['message']}")
            print(f"            why it matters: {f['why']}")

    def cmd_recover(self, args: List[str]) -> None:
        """Run recovery."""
        print("Running recovery...")
        res = self.fs.recover()
        print(res)
        for r in res.replayed:
            print(f"  replayed tx {r['txid']} -> blocks {r['blocks']}")
        for d in res.discarded:
            print(f"  discarded tx {d['txid']}: {d['reason']}")
        for w in res.warnings:
            print(f"  warning: {w}")

    def cmd_journal(self, args: List[str]) -> None:
        v = self.fs.journal_view()
        st = v['stats']
        print(f"journal: {st['journal_blocks']} blocks, head={st['head']} tail={st['tail']} "
              f"checkpoint_txid={st['checkpoint_txid']} next_txid={st['next_txid']}")
        for t in v['transactions'][-20:]:
            kinds = ','.join(r['type'].replace('TRANS_', '') for r in t['records'])
            print(f"  tx {t['txid']:>4} blocks {t['start_block']}+{t['blocks']}  {t['state']:<24} [{kinds}]")

    def cmd_inode(self, args: List[str]) -> None:
        import json
        print(json.dumps(self.fs.inode_info(int(args[0])), indent=2))

    def cmd_block(self, args: List[str]) -> None:
        import json
        info = self.fs.block_info(int(args[0]))
        print(json.dumps(info, indent=2, default=str))

    def cmd_layout(self, args: List[str]) -> None:
        for r in self.fs.layout():
            print(f"{r['name']:<12} blocks {r['start_block']:>6}-{r['end_block']:<6} "
                  f"offset {r['offset_bytes']:>10}  size {r['size_bytes']:>10}  used {r['used_blocks']}")

    def cmd_events(self, args: List[str]) -> None:
        import time as _t
        n = int(args[0]) if args else 30
        for e in self.fs.events.snapshot()[-n:]:
            tx = f"tx{e['txid']}" if e['txid'] else '   '
            print(f"{_t.strftime('%H:%M:%S', _t.localtime(e['ts']))} {e['severity']:<7} {e['kind']:<16} {tx:<6} {e['message']}")

    def cmd_crash_test(self, args: List[str]) -> None:
        """Run a REAL crash scenario (separate processes, SIGKILL) on a temporary image."""
        from .crash_lab import run_scenario
        injector = CrashInjector()
        if not args:
            print("Usage: crash-test <scenario> [operation]")
            print("Scenarios:")
            for s in injector.get_scenarios():
                print(f"  {s.name}: {s.description}")
            print("Operations: " + ", ".join(LAB_OPERATIONS))
            return
        operation = args[1] if len(args) > 1 else "create_file"
        scenario = injector.get_scenario(args[0])
        if not scenario:
            print(f"Unknown scenario: {args[0]}")
            return
        print(f"Scenario : {scenario.name} - {scenario.description}")
        print(f"Operation: {LAB_OPERATIONS[operation]['label']}")
        print("(runs on a temporary image; your disk image is not touched)")
        r = run_scenario(scenario.name, operation)
        for st in r["steps"]:
            print(f"  step: {st['step']}  (killed={st.get('killed')}, rc={st.get('returncode')})")
        print(f"Expected after recovery: {r['expected_state'].upper()}")
        print(f"Actual   after recovery: {'operation SURVIVED' if r.get('survived') else 'operation ABSENT'}")
        for name, ok in (r.get('checks') or {}).items():
            print(f"  [{'ok' if ok else 'FAIL'}] {name}")
        print(f"VERDICT: {r['verdict']}")

    def cmd_enable_crash_injection(self, args: List[str]) -> None:
        """Enable crash injection."""
        if not args:
            print("Usage: enable-crash-injection <mode>")
            print("Modes: exception, subprocess, none")
            return
        
        mode_str = args[0].lower()
        if mode_str == 'exception':
            mode = CrashMode.EXCEPTION
        elif mode_str == 'subprocess':
            mode = CrashMode.SUBPROCESS
        elif mode_str == 'none':
            mode = CrashMode.NONE
        else:
            print(f"Unknown mode: {mode_str}")
            print("Modes: exception, subprocess, none")
            return
        
        self.fs.enable_crash_injection(mode)
        print(f"Crash injection enabled: {mode.value}")
    
    def cmd_disable_crash_injection(self, args: List[str]) -> None:
        """Disable crash injection."""
        self.fs.disable_crash_injection()
        print("Crash injection disabled")
    
    
def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="CRFS Filesystem Simulator")
    parser.add_argument('disk', nargs='?', default='filesystem.img',
                       help='Disk image path (default: filesystem.img)')
    parser.add_argument('-c', '--command', help='Single command to run')
    parser.add_argument('--format', action='store_true', help='Format new filesystem')
    parser.add_argument('--size', default='16M', help='Disk size for format (default: 16M)')
    parser.add_argument('--journal-blocks', type=int, default=128, help='Journal blocks for format (default: 128)')
    parser.add_argument('--inode-blocks', type=int, default=64, help='Inode blocks for format (default: 64)')
    parser.add_argument('--label', default='', help='Volume label for format (default: empty)')
    parser.add_argument('--no-recovery', action='store_true', help='Mount read-only without running recovery')
    parser.add_argument('--cache-mode', choices=['volatile', 'writethrough'], default='volatile',
                        help='Disk durability model (default: volatile write-back cache)')
    parser.add_argument('--checkpoint', choices=['eager', 'lazy'], default='eager',
                        help='Checkpoint after every transaction, or lazily when the journal fills')
    
    args = parser.parse_args()
    
    fs = FileSystem(args.disk, cache_mode=args.cache_mode, checkpoint=args.checkpoint)
    cli = CLI(fs)
    exit_code = 0
    
    try:
        if args.format or args.command == 'format':
            # Format and exit
            size_str = args.size
            if size_str.endswith('K'):
                disk_size = int(size_str[:-1]) * 1024
            elif size_str.endswith('M'):
                disk_size = int(size_str[:-1]) * 1024 * 1024
            elif size_str.endswith('G'):
                disk_size = int(size_str[:-1]) * 1024 * 1024 * 1024
            else:
                disk_size = int(size_str)
            
            fs.format(disk_size=disk_size, journal_blocks=args.journal_blocks, 
                     inode_blocks=args.inode_blocks, volume_label=args.label)
            print(f"Formatted {args.disk} ({disk_size} bytes)")
            return
        
        if args.command:
            # Run single command
            fs.mount(run_recovery=not args.no_recovery)
            cmd_args = shlex.split(args.command)
            exit_code = cli.run_command(cmd_args)
            fs.unmount()
            sys.exit(exit_code)
        else:
            # Interactive mode - auto-mount if exists
            import os
            if os.path.exists(args.disk):
                fs.mount(run_recovery=not args.no_recovery)
            cli.run_interactive()
    finally:
        if fs.is_mounted:
            fs.unmount()


if __name__ == '__main__':
    main()