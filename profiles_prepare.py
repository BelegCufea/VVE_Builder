#!/usr/bin/env python3
"""
Voice Sample Preparation Tool for Infinity Engine Games
Processes dialog-report.csv and extracts audio samples for voice generation
"""

import csv
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field
from collections import defaultdict
import shutil
import logging

# Configuration
MIN_DURATION = 15.0  # Minimum duration in seconds (sum of samples)
MAX_DURATION = 30.0  # Maximum duration in seconds (single sample file)
WEIDU_PATH = r"./weidu/weidu.exe"
GAME_DIRECTORY = r"C:/Relax/BGEET"
CSV_PATH = r"dialog-report.csv"
APP_DIR = Path.cwd()  # Directory where the script is running
VOICES_PREP_DIR = APP_DIR / "voices_prep"
VOICES_DIR = APP_DIR / "voices"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


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
        """Check if SoundResRef starts with TS (TTS-generated, needs regeneration)"""
        return self.sound_res_ref.startswith('TS')
    
    @property
    def is_digit_only(self) -> bool:
        """Check if SoundResRef contains only digits (lower priority)"""
        return self.sound_res_ref.isdigit()
    
    @property
    def has_brackets(self) -> bool:
        """Check if text contains angle brackets (very low priority)"""
        return '<' in self.text and '>' in self.text
    
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
        """Check if this entry can be used as a sample (NON-TS)"""
        return not self.is_ts_sound


class VoiceSampleProcessor:
    """Main processor for voice sample extraction"""
    
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self.entries_by_realname: Dict[str, List[DialogEntry]] = defaultdict(list)
        self.existing_voices: Set[str] = set()
        self.processed_groups: Set[str] = set()
        
    def load_csv(self) -> None:
        """Load and parse the CSV file"""
        try:
            with open(self.csv_path, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    # Skip rows without RealName
                    if not row.get('RealName'):
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
            
            logger.info(f"Loaded {sum(len(v) for v in self.entries_by_realname.values())} entries for {len(self.entries_by_realname)} characters (with TS entries)")
            
            # Log sample availability
            for name, entries in self.entries_by_realname.items():
                sample_entries = [e for e in entries if e.is_valid_sample]
                if not sample_entries:
                    logger.warning(f"Character {name} has TS entries but NO sample entries available!")
                    
        except Exception as e:
            logger.error(f"Failed to load CSV: {e}")
            sys.exit(1)
    
    def scan_existing_voices(self) -> None:
        """Scan voices and voices_prep directories for existing files"""
        for dir_path in [VOICES_DIR, VOICES_PREP_DIR]:
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
        extract_dir = VOICES_PREP_DIR / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        results = {}
        to_extract = []  # Files that need weidu extraction
        override_count = 0
        
        for entry in entries:
            sound_res_ref = entry.sound_res_ref
            output_path = extract_dir / f"{sound_res_ref}.WAV"
            
            # Check override folder first
            override_path = Path(GAME_DIRECTORY) / "override" / f"{sound_res_ref}.WAV"
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
            game_path = str(Path(GAME_DIRECTORY))
            
            try:
                weidu_exe = Path(WEIDU_PATH)
                if not weidu_exe.exists():
                    logger.error(f"Weidu not found at: {WEIDU_PATH}")
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
            logger.info(f"Skipping {real_name} - already has voice files")
            return False
        
        # Check if character has TS entries
        has_ts = any(e.is_ts_sound for e in entries)
        if not has_ts:
            logger.debug(f"Skipping {real_name} - no TS entries to regenerate")
            return False
        
        # Get sample entries (NON-TS)
        sample_entries = [e for e in entries if e.is_valid_sample]
        if not sample_entries:
            logger.warning(f"Character {real_name} has TS entries but NO non-TS sample entries available!")
            return False
        
        # Sort sample entries by priority and text length
        entries_by_priority = defaultdict(list)
        for entry in sample_entries:
            entries_by_priority[entry.priority].append(entry)
        
        # Process each priority level
        collected_samples = []
        total_duration = 0.0
        priority_used = 2
        
        for priority in sorted(entries_by_priority.keys()):
            # Get top 5 longest texts for this priority
            sorted_entries = sorted(
                entries_by_priority[priority],
                key=lambda e: len(e.text),
                reverse=True
            )[:5]  # Only take top 5
            
            if not sorted_entries:
                continue
            
            # Mass extract audio files for these entries
            logger.debug(f"Extracting {len(sorted_entries)} samples for {real_name} (priority {priority})")
            extracted_paths = self.extract_audio_files(sorted_entries)
            
            # Now build a list of valid samples with their durations
            valid_samples = []
            for entry in sorted_entries:
                audio_path = extracted_paths.get(entry.sound_res_ref)
                if not audio_path:
                    continue
                
                duration = self.get_audio_duration(audio_path)
                if duration is None:
                    continue
                
                # Reject samples longer than MAX_DURATION
                if duration > MAX_DURATION:
                    logger.debug(f"Rejected {entry.sound_res_ref} - too long ({duration:.2f}s > {MAX_DURATION}s)")
                    continue
                
                valid_samples.append((entry, audio_path, duration))
            
            # Sort valid samples by duration (we want to use shorter ones first to avoid exceeding limits,
            # but we also want to reach MIN_DURATION)
            # Let's sort by priority and duration ascending
            valid_samples.sort(key=lambda x: (x[0].priority, -x[2]))
            
            # Add samples until we reach MIN_DURATION or run out
            for entry, audio_path, duration in valid_samples:
                if total_duration >= MIN_DURATION:
                    break
                
                collected_samples.append((entry, audio_path, duration))
                total_duration += duration
                priority_used = priority
                logger.debug(f"Added sample {entry.sound_res_ref} ({duration:.2f}s) - total: {total_duration:.2f}s")
            
            if total_duration >= MIN_DURATION:
                break
        
        if not collected_samples:
            logger.warning(f"No usable samples found for {real_name}")
            return False
        
        # Check if we have enough duration
        if total_duration < MIN_DURATION:
            logger.warning(f"{real_name} - Only {total_duration:.2f}s available (need {MIN_DURATION}s)")
        
        # Create output files
        return self.create_sample_files(real_name, collected_samples, priority_used, total_duration, has_ts)
    
    def create_sample_files(self, real_name: str, samples: List[Tuple], priority_used: int, total_duration: float, has_ts: bool) -> bool:
        """Create WAV and TXT files for the collected samples"""
        # Create voices_prep directory if it doesn't exist
        VOICES_PREP_DIR.mkdir(parents=True, exist_ok=True)
        
        priority_labels = ["high", "medium", "low"]
        priority_text = priority_labels[priority_used] if priority_used is not None else "unknown"
        
        # Log the sample creation
        if total_duration >= MIN_DURATION:
            logger.info(f"✅ Creating sample for {real_name} using {len(samples)} samples ({total_duration:.2f}s, priority: {priority_text})")
        else:
            logger.warning(f"⚠️ Creating incomplete sample for {real_name} - only {total_duration:.2f}s available (need {MIN_DURATION}s)")
        
        # Create files for each sample
        for idx, (entry, audio_path, duration) in enumerate(samples):
            if idx == 0:
                base_name = real_name
            else:
                base_name = f"{real_name} {idx + 1}"
            
            # Copy WAV file from extracted directory to voices_prep
            wav_path = VOICES_PREP_DIR / f"{base_name}.WAV"
            shutil.copy2(audio_path, wav_path)
            logger.debug(f"Copied {wav_path}")
            
            # Create TXT file with text
            txt_path = VOICES_PREP_DIR / f"{base_name}.txt"
            with open(txt_path, 'w', encoding='utf-8') as f:
                f.write(entry.text)
            logger.debug(f"Created {txt_path}")
        
        # Create a summary file
        summary_path = VOICES_PREP_DIR / f"{real_name}_summary.txt"
        with open(summary_path, 'w', encoding='utf-8') as f:
            f.write(f"Character: {real_name}\n")
            f.write(f"Has TS Entries: {has_ts}\n")
            f.write(f"Total Duration: {total_duration:.2f}s\n")
            f.write(f"Priority Used: {priority_text}\n")
            f.write(f"Number of Samples: {len(samples)}\n")
            f.write("\nSamples:\n")
            for idx, (entry, audio_path, duration) in enumerate(samples):
                f.write(f"  {idx+1}. {entry.sound_res_ref}: {duration:.2f}s\n")
                f.write(f"     Text: {entry.text[:100]}...\n")
            f.write(f"\nStatus: {'✅ SUFFICIENT' if total_duration >= MIN_DURATION else '⚠️ INSUFFICIENT'}\n")
        
        return True
    
    def process_all(self) -> None:
        """Process all character groups"""
        self.load_csv()
        self.scan_existing_voices()
        
        # Sort characters by number of entries (more entries = more likely to find good samples)
        sorted_groups = sorted(
            self.entries_by_realname.items(),
            key=lambda x: len(x[1]),
            reverse=True
        )
        
        processed_count = 0
        skipped_count = 0
        no_samples_count = 0
        
        for real_name, entries in sorted_groups:
            # Check if character has any sample entries
            has_samples = any(e.is_valid_sample for e in entries)
            
            if real_name in self.existing_voices:
                skipped_count += 1
                continue
            
            if not has_samples:
                logger.warning(f"Skipping {real_name} - has TS entries but no non-TS sample entries")
                no_samples_count += 1
                continue
                
            if self.process_character_group(real_name, entries):
                processed_count += 1
                # Add to existing voices to prevent duplicate processing
                self.existing_voices.add(real_name)
        
        logger.info(f"\n{'='*50}")
        logger.info(f"Processing complete!")
        logger.info(f"  ✅ Processed: {processed_count} characters")
        logger.info(f"  ⏭️ Skipped (already exists): {skipped_count} characters")
        logger.info(f"  ⚠️ No samples available: {no_samples_count} characters")
        logger.info(f"  📁 Files created in: {VOICES_PREP_DIR}")
        extract_dir = VOICES_PREP_DIR / "extracted"
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
            logger.debug("  Cleaned up extracted directory")        
        logger.info(f"{'='*50}")


def main():
    """Main entry point"""
    csv_path = Path(CSV_PATH)
    if not csv_path.exists():
        print(f"Error: CSV file not found: {csv_path}")
        sys.exit(1)
    
    # Validate game directory
    if not Path(GAME_DIRECTORY).exists():
        logger.warning(f"Game directory not found: {GAME_DIRECTORY}")
        logger.warning("Please update GAME_DIRECTORY to your actual game directory path")
    
    # Validate weidu
    weidu_path = Path(WEIDU_PATH)
    if not weidu_path.exists():
        logger.warning(f"Weidu not found at: {WEIDU_PATH}")
        logger.warning("Please update WEIDU_PATH to point to your weidu executable")
    
    # Create output directories
    VOICES_PREP_DIR.mkdir(parents=True, exist_ok=True)
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    
    # Process
    processor = VoiceSampleProcessor(csv_path)
    processor.process_all()


if __name__ == "__main__":
    main()