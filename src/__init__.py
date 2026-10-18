"""
CRFS - Crash-Recoverable File System Simulator

A Python implementation of a crash-recoverable file system with
write-ahead journaling for an Operating Systems course project.
"""

from .virtual_disk import VirtualDisk, create_disk_image, open_disk_image, CrashPoint
from .superblock import Superblock, SuperblockManager, MAGIC_NUMBER, FORMAT_VERSION
from .block_allocator import BlockAllocator, BlockAllocatorError, NoFreeBlocksError
from .inode import Inode, InodeTable, InodeType, InodeError, InodeTableFullError
from .directory import Directory, DirEntry, DirectoryError, EntryNotFoundError
from .journal import Journal, Transaction, RecordType, JournalError, JournalFullError, TransBeginRecord, TransDataRecord, TransCommitRecord, CheckpointRecord
from .transaction import TransactionManager, TransactionContext, TransactionError
from .recovery import RecoveryManager, RecoveryResult, run_recovery
from .consistency_checker import ConsistencyChecker, CheckResult, run_consistency_check
from .crash_injector import CrashInjector, CrashMode, CrashScenario, CrashInjectedError
from .filesystem import FileSystem, create_filesystem, open_filesystem, FileSystemError, DirectoryNotEmptyError
from .cli import CLI, main

__version__ = "1.0.0"
__author__ = "OS Course Project"

__all__ = [
    # Virtual disk
    "VirtualDisk", "create_disk_image", "open_disk_image", "CrashPoint",
    # Superblock
    "Superblock", "SuperblockManager", "MAGIC_NUMBER", "FORMAT_VERSION",
    # Block allocator
    "BlockAllocator", "BlockAllocatorError", "NoFreeBlocksError",
    # Inode
    "Inode", "InodeTable", "InodeType", "InodeError", "InodeTableFullError",
    # Directory
    "Directory", "DirEntry", "DirectoryError", "EntryNotFoundError", "DirectoryNotEmptyError",
    # Journal
    "Journal", "Transaction", "RecordType", "JournalError", "JournalFullError",
    "TransBeginRecord", "TransDataRecord", "TransCommitRecord", "CheckpointRecord",
    # Transaction
    "TransactionManager", "TransactionContext", "TransactionError",
    # Recovery
    "RecoveryManager", "RecoveryResult", "run_recovery",
    # Consistency checker
    "ConsistencyChecker", "CheckResult", "run_consistency_check",
    # Crash injector
    "CrashInjector", "CrashMode", "CrashScenario", "CrashInjectedError",
    # Filesystem
    "FileSystem", "create_filesystem", "open_filesystem", "FileSystemError",
    # CLI
    "CLI", "main",
]