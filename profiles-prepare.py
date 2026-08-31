#!/usr/bin/env python3
"""
Voice Sample Preparation Tool for Infinity Engine Games
Processes dialog-report.csv and extracts audio samples for voice generation
"""

import csv
import subprocess
import sys
import io
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass
from collections import defaultdict
import shutil
import logging

from appconfig import cfg
from utils import setup_logging


# Ensure UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# Setup logging
logger = setup_logging(Path(__file__).stem)


@dataclass
class DialogEntry:
    """Represents a single dialog entry from the CSV"""
    str_ref: str
    system_name: str
    real_name: str
    gender: str
    has_sound: bool
    sound_res_ref: str
    sound_file_exists: bool
    text: str
    
    @property
    def is_ts_sound(self) -> bool:
        """
        Check if this entry needs TTS regeneration.
        Returns True if:
        - SoundResRef is empty (no original voice)
        - SoundResRef matches the TS pattern (TTS-generated)
        """
        # Empty SoundResRef means no original voice file exists
        if not self.sound_res_ref or self.sound_res_ref.strip() == '':
            return True
        
        # Check if SoundResRef starts with the configured prefix (case-insensitive)
        return bool(self.sound_res_ref) and self.sound_res_ref.upper().startswith(cfg.FILENAME_PREFIX.upper())
    
    @property
    def is_digit_only(self) -> bool:
        """Check if SoundResRef contains only digits (lower priority)"""
        return self.sound_res_ref.isdigit() if self.sound_res_ref else False
    
    @property
    def has_brackets(self) -> bool:
        """Check if text contains angle brackets (very low priority)"""
        return '<' in self.text and '>' in self.text if self.text else False
    
    @property
    def priority(self) -> int:
        """
        Priority ranking for SAMPLE entries (NON-TS):
        0: Highest - non-digit SoundResRef without <>
        1: Medium - digit-only SoundResRef without <>
        2: Lowest - contains <>
        """
        if self.has_brackets:
            return 2
        elif self.is_digit_only:
            return 1
        else:
            return 0
    
    @property
    def is_valid_sample(self) -> bool:
        """
        Check if this entry can be used as a sample (NON-TS).
        Returns True if it's NOT a TS entry.
        """
        return not self.is_ts_sound


class VoiceSampleProcessor:
    """Main processor for voice sample extraction"""
    
    def __init__(self, csv_path: Path, blacklist: Optional[Set[str]] = None):
        self.csv_path = csv_path
        self.blacklist = blacklist or set()
        self.entries_by_realname: Dict[str, List[DialogEntry]] = defaultdict(list)
        self.existing_voices: Set[str] = set()
        self.processed_groups: Set[str] = set()
        
    def load_csv(self) -> None:
        """Load and parse the CSV file"""
        try:
            with open(self.csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    if not row.get('RealName'):
                        continue
                    
                    # Skip blacklisted characters
                    if row['RealName'] in self.blacklist:
                        continue
                    
                    entry = DialogEntry(
                        str_ref=row.get('StrRef', ''),
                        system_name=row.get('SystemName', ''),
                        real_name=row['RealName'],
                        gender=row.get('Gender', ''),
                        has_sound=row.get('HasSound', 'false').lower() == 'true',
                        sound_res_ref=row.get('SoundResRef', ''),
                        sound_file_exists=row.get('SoundFileExists', 'false').lower() == 'true',
                        text=row.get('Text', '')
                    )
                    
                    self.entries_by_realname[entry.real_name].append(entry)
            
            # Filter: Only keep characters that have at least one TS entry
            characters_with_ts = {
                name: entries 
                for name, entries in self.entries_by_realname.items()
                if any(e.is_ts_sound for e in entries)
            }
            
            self.entries_by_realname = characters_with_ts
            
            # Count characters with and without samples
            total_chars = len(self.entries_by_realname)
            chars_with_samples = sum(1 for entries in self.entries_by_realname.values() if any(e.is_valid_sample for e in entries))
            chars_without_samples = total_chars - chars_with_samples
            
            logger.info(f"📊 Loaded {sum(len(v) for v in self.entries_by_realname.values())} entries for {total_chars} characters (need TTS regeneration)")
            if chars_without_samples > 0:
                logger.warning(f"⚠️ {chars_without_samples} characters have NO source samples available")
                
            if self.blacklist:
                logger.info(f"🚫 {len(self.blacklist)} characters blacklisted (skipped)")
                    
        except Exception as e:
            logger.error(f"Failed to load CSV: {e}")
            sys.exit(1)

    def scan_existing_voices(self) -> None:
        """Scan voices and voices_prep directories for existing files"""
        voices_dir = Path(cfg.VOICES_DIR)
        voices_prep_dir = Path(cfg.VOICES_PREP_DIR)
        for dir_path in [voices_dir, voices_prep_dir]:
            if dir_path.exists():
                for file in dir_path.glob("*.wav"):
                    # Extract RealName from filename (without number suffix)
                    name = file.stem
                    # Remove number suffix if present (e.g., "Name 2" -> "Name")
                    if ' ' in name and name.split(' ')[-1].isdigit():
                        name = ' '.join(name.split(' ')[:-1])
                    self.existing_voices.add(name)
                    
        if self.existing_voices:
            logger.info(f"Found {len(self.existing_voices)} existing voice samples")
    
    def get_audio_duration(self, audio_path: Path) -> Optional[float]:
        """Get duration of audio file using ffmpeg"""
        try:
            cmd = [
                'ffprobe', 
                '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                str(audio_path)
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                return float(result.stdout.strip())
            return None
        except Exception as e:
            logger.error(f"Failed to get duration for {audio_path}: {e}")
            return None
    
    def extract_audio_files(self, entries: List[DialogEntry]) -> Dict[str, Optional[Path]]:
        """
        Mass extract audio files for multiple entries.
        Returns dict mapping sound_res_ref to Path or None if extraction failed.
        """
        extract_dir = Path(cfg.VOICES_PREP_DIR) / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        results = {}
        to_extract = []  # Files that need weidu extraction
        override_count = 0
        
        for entry in entries:
            sound_res_ref = entry.sound_res_ref
            output_path = extract_dir / f"{sound_res_ref}.WAV"
            
            # Check override folder first
            override_path = Path(cfg.GAME_DIRECTORY) / "override" / f"{sound_res_ref}.WAV"
            if override_path.exists():
                # Copy from override to extracted directory
                shutil.copy2(override_path, output_path)
                results[sound_res_ref] = output_path
                override_count += 1
                logger.debug(f"Found in override: {sound_res_ref}")
            else:
                # Need to extract via weidu
                to_extract.append(sound_res_ref)
                results[sound_res_ref] = None  # Placeholder
        
        # Mass extract using weidu
        if to_extract:
            logger.info(f"Extracting {len(to_extract)} files with WeiDU...")
            game_path = str(Path(cfg.GAME_DIRECTORY))
            
            try:
                weidu_exe = Path(cfg.WEIDU_PATH)
                if not weidu_exe.exists():
                    logger.error(f"Weidu not found at: {cfg.WEIDU_PATH}")
                    # Mark all as failed
                    for sound_res_ref in to_extract:
                        results[sound_res_ref] = None
                    return results
                
                # Build command with multiple --biff-get parameters
                cmd = [str(weidu_exe), '--game', game_path, '--out', str(extract_dir)]
                for sound_res_ref in to_extract:
                    cmd.extend(['--biff-get', f"{sound_res_ref}.WAV"])
                
                logger.debug(f"Running: {' '.join(cmd)}")
                result = subprocess.run(cmd, capture_output=True, text=True)
                
                # Check each extracted file
                for sound_res_ref in to_extract:
                    output_path = extract_dir / f"{sound_res_ref}.WAV"
                    if output_path.exists():
                        results[sound_res_ref] = output_path
                        logger.debug(f"Extracted: {sound_res_ref}")
                    else:
                        logger.warning(f"Failed to extract: {sound_res_ref}")
                        
            except Exception as e:
                logger.error(f"Error during mass extraction: {e}")
                # Mark all as failed
                for sound_res_ref in to_extract:
                    results[sound_res_ref] = None
        
        logger.info(f"Extraction complete: {override_count} from override, {len([p for p in results.values() if p]) - override_count} from BIFF, {len([p for p in results.values() if not p])} failed")
        return results
    
    def process_character_group(self, real_name: str, entries: List[DialogEntry]) -> bool:
        """Process a single character group and create voice sample"""
        if real_name in self.existing_voices:
            return False
        
        # Check if character has TS entries
        has_ts = any(e.is_ts_sound for e in entries)
        if not has_ts:
            return False
        
        # Get sample entries (NON-TS)
        sample_entries = [e for e in entries if e.is_valid_sample]
        if not sample_entries:
            return False
        
        # Log that we're starting to process this character
        logger.debug(f"Processing {real_name} - found {len(sample_entries)} potential samples")
        
        # Sort sample entries by priority and text length
        entries_by_priority = defaultdict(list)
        for entry in sample_entries:
            entries_by_priority[entry.priority].append(entry)
        
        # Process each priority level
        collected_samples = []
        total_duration = 0.0
        priority_used = 2  # Default to lowest
        priority_names = {0: "HIGH", 1: "MEDIUM", 2: "LOW"}
        
        for priority in sorted(entries_by_priority.keys()):
            # Get top 5 longest texts for this priority
            sorted_entries = sorted(
                entries_by_priority[priority],
                key=lambda e: len(e.text),
                reverse=True
            )[:5]
            
            if not sorted_entries:
                continue
            
            # Mass extract audio files for these entries
            logger.debug(f"Extracting {len(sorted_entries)} samples for {real_name} (priority {priority_names[priority]})")
            extracted_paths = self.extract_audio_files(sorted_entries)
            
            # Build list of valid samples
            valid_samples = []
            for entry in sorted_entries:
                audio_path = extracted_paths.get(entry.sound_res_ref)
                if not audio_path:
                    continue
                
                duration = self.get_audio_duration(audio_path)
                if duration is None:
                    continue
                
                if duration > cfg.MAX_DURATION:
                    logger.debug(f"Rejected {entry.sound_res_ref} - too long ({duration:.2f}s > {cfg.MAX_DURATION}s)")
                    continue
                
                valid_samples.append((entry, audio_path, duration))
            
            # Sort by priority ascending, then duration descending
            valid_samples.sort(key=lambda x: (x[0].priority, -x[2]))
            
            # Add samples until we reach cfg.MIN_DURATION
            for entry, audio_path, duration in valid_samples:
                if total_duration >= cfg.MIN_DURATION:
                    break
                
                collected_samples.append((entry, audio_path, duration))
                total_duration += duration
                priority_used = priority
                logger.debug(f"Added sample {entry.sound_res_ref} ({duration:.2f}s) - total: {total_duration:.2f}s")
            
            if total_duration >= cfg.MIN_DURATION:
                break
        
        if not collected_samples:
            logger.warning(f"❌ No usable samples found for {real_name}")
            return False
        
        # Create output files
        return self.create_sample_files(real_name, collected_samples, priority_used, total_duration, has_ts)
    
    def create_sample_files(self, real_name: str, samples: List[Tuple], priority_used: int, total_duration: float, has_ts: bool) -> bool:
        """Create WAV and TXT files for the collected samples"""
        voice_prep_dir = Path(cfg.VOICES_PREP_DIR)
        
        priority_labels = ["HIGH", "MEDIUM", "LOW"]
        priority_text = f"{'⚠️ ' if priority_used > 0 else ''}{priority_labels[priority_used] if priority_used is not None else 'HIGH'}"
        
        # Build status message
        status = "✅ " if total_duration >= cfg.MIN_DURATION else "⚠️ "
        duration_status = f"{total_duration:.1f}s" + (f" (need {cfg.MIN_DURATION:.1f}s)" if total_duration < cfg.MIN_DURATION else "")
        
        # Build StrRef:SoundResRef pairs for logging
        sample_pairs = [f"{entry.str_ref}:{entry.sound_res_ref}({duration:.1f}s)" for entry, _, duration in samples]
        pairs_str = ", ".join(sample_pairs) if len(sample_pairs) <= 3 else f"{', '.join(sample_pairs[:3])}... ({len(sample_pairs)} total)"
        
        logger.log(logging.WARNING if priority_used > 0 or total_duration < cfg.MIN_DURATION else logging.INFO,  f"{status} {real_name}: {len(samples)} samples, {duration_status} [{priority_text} priority] [{pairs_str}]")
        
        # Detailed debug log for all samples
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(f"  Detailed samples for {real_name}:")
            for idx, (entry, audio_path, duration) in enumerate(samples):
                logger.debug(f"    {idx+1}. StrRef: {entry.str_ref}, Sound: {entry.sound_res_ref}, Duration: {duration:.2f}s")
        
        # Create files for each sample - ALWAYS use numbers starting from 1
        for idx, (entry, audio_path, duration) in enumerate(samples):
            # Always use number, starting from 1
            sample_num = idx + 1
            if sample_num == 1:
                base_name = f"{real_name} 1"
            else:
                base_name = f"{real_name} {sample_num}"
            
            # Copy WAV file
            wav_path = voice_prep_dir / f"{base_name}.WAV"
            shutil.copy2(audio_path, wav_path)
            
            # Create TXT file with text
            txt_path = voice_prep_dir / f"{base_name}.txt"
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write(entry.text)
        
        return True

    def can_create_file(self, path: Path) -> bool:
        """
        Check if a file can be created at the given path.
        Returns True if the file can be created, False otherwise.
        """
        try:
            # Try to create the file (fail if it already exists)
            path.touch(exist_ok=False)
            path.unlink()  # Clean up immediately
            return True
        except OSError:
            return False
    
    def process_all(self) -> None:
        """Process all character groups"""
        self.load_csv()
        self.scan_existing_voices()

        voice_prep_dir = Path(cfg.VOICES_PREP_DIR)
        
        # Filter and prepare the list of characters to process
        characters_to_process = []
        skipped_count = 0
        no_samples_count = 0
        blacklisted_count = 0
        invalid_filename_count = 0
        
        for real_name, entries in self.entries_by_realname.items():
            # Check if blacklisted
            if real_name in self.blacklist:
                blacklisted_count += 1
                continue
                
            # Check if already exists
            if real_name in self.existing_voices:
                skipped_count += 1
                continue
            
            # Check if has sample entries
            has_samples = any(e.is_valid_sample for e in entries)
            if not has_samples:
                no_samples_count += 1
                continue

            # Check if we can create a file with this RealName
            test_path = voice_prep_dir / f"{real_name}.WAV"
            if not self.can_create_file(test_path):
                logger.warning(f"❌ Skipping '{real_name}' - cannot create file (invalid filename or permissions)")
                invalid_filename_count += 1
                continue            
            
            # This character is eligible for processing
            characters_to_process.append((real_name, entries))
        
        # Sort characters by number of entries (more entries = more likely to find good samples)
        characters_to_process.sort(key=lambda x: len(x[1]), reverse=True)
        
        total_to_process = len(characters_to_process)
        
        logger.info(f"\n{'─'*50}")
        logger.info(f"🎯 Starting voice sample preparation...")
        logger.info(f"{'─'*50}")
        logger.info(f"  📊 Total characters with TS entries: {len(self.entries_by_realname)}")
        logger.info(f"  🚫 Blacklisted: {blacklisted_count}")
        logger.info(f"  ⏭️ Already exist: {skipped_count}")
        logger.info(f"  ⚠️ No source samples: {no_samples_count}")
        logger.info(f"  ❌ Invalid filenames: {invalid_filename_count}")        
        logger.info(f"  ✅ To process: {total_to_process}")
        logger.info(f"{'─'*50}")
        
        processed_count = 0
        
        for idx, (real_name, entries) in enumerate(characters_to_process, 1):
            # Log progress before processing
            logger.info(f"[{idx}/{total_to_process}] Processing: {real_name}")
            
            if self.process_character_group(real_name, entries):
                processed_count += 1
                self.existing_voices.add(real_name)
        
        # Clean up extracted directory
        extract_dir = voice_prep_dir / "extracted"
        if extract_dir.exists():
            try:
                shutil.rmtree(extract_dir)
                logger.debug("Cleaned up extracted directory")
            except PermissionError:
                logger.warning(f"⚠️ Could not delete {extract_dir} - permission denied (files may be in use)")
            except Exception as e:
                logger.warning(f"⚠️ Could not delete {extract_dir}: {e}")
        
        # Final summary
        logger.info(f"\n{'='*60}")
        logger.info(f"📋 PROCESSING COMPLETE")
        logger.info(f"{'='*60}")
        logger.info(f"  ✅ Processed:          {processed_count:>3} / {total_to_process} characters")
        logger.info(f"  ⏭️  Already exist:      {skipped_count:>3} characters")
        logger.info(f"  ⚠️  No source samples:  {no_samples_count:>3} characters")
        logger.info(f"  🚫 Blacklisted:        {blacklisted_count:>3} characters")
        logger.info(f"  📁 Output directory:   {cfg.VOICES_PREP_DIR}")
        logger.info(f"{'='*60}")

def load_blacklist(blacklist_file: Optional[Path] = None) -> Set[str]:
    """Load blacklist from a file, one name per line"""
    blacklist = set(cfg.BLACKLIST)  # Start with hardcoded list
    
    if blacklist_file and blacklist_file.exists():
        with open(blacklist_file, 'r', encoding='utf-8') as f:
            for line in f:
                name = line.strip()
                if name and not name.startswith('#'):  # Skip comments and empty lines
                    blacklist.add(name)
        logger.info(f"Loaded {len(blacklist)} blacklisted characters from {blacklist_file}")
    
    return blacklist

def main():
    """Main entry point"""
    csv_path = Path(cfg.CSV_PATH)
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}")
        sys.exit(1)
    
    # Validate game directory
    if not Path(cfg.GAME_DIRECTORY).exists():
        logger.warning(f"Game directory not found: {cfg.GAME_DIRECTORY}")
        logger.warning("Please update cfg.GAME_DIRECTORY to your actual game directory path")
    
    # Validate weidu
    weidu_path = Path(cfg.WEIDU_PATH)
    if not weidu_path.exists():
        logger.warning(f"Weidu not found at: {cfg.WEIDU_PATH}")
        logger.warning("Please update cfg.WEIDU_PATH to point to your weidu executable")
    
    # Create output directories
    voices_dir = Path(cfg.VOICES_DIR)
    voices_prep_dir = Path(cfg.VOICES_PREP_DIR)
    voices_dir.mkdir(parents=True, exist_ok=True)
    voices_prep_dir.mkdir(parents=True, exist_ok=True)

    # Load blacklist
    blacklist_file = Path(blacklist_file) if (blacklist_file := Path(cfg.BLACKLIST_FILE)).exists() else None
    blacklist = load_blacklist(blacklist_file)
    
    # Process
    processor = VoiceSampleProcessor(csv_path, blacklist)
    processor.process_all()


if __name__ == "__main__":
    main()