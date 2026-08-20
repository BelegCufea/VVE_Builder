import os
import json
import uuid
import shutil
import zipfile
import requests
from pathlib import Path
from collections import defaultdict
import re

# ============================================================================
# CONFIGURATION - Modify these values as needed
# ============================================================================

# API Configuration
API_BASE_URL = "http://10.0.50.5:17600"   # VoiceBox REST API endpoint - http://localhost:17493 for local server, or remote URL for remote server
PROFILES_ENDPOINT = "/profiles"           # API endpoint for profile listing
IMPORT_ENDPOINT = "/profiles/import"      # API endpoint for profile import

# Directory Configuration
VOICES_DIR = "voices"                     # Directory containing voice files
OUTPUT_DIR = "profiles"                   # Directory for profile packages

# Import Configuration
AUTO_IMPORT = True                        # Automatically import new profiles
SKIP_EXISTING = True                      # Skip profiles that already exist

# File Patterns
WAV_EXTENSIONS = ['.WAV', '.wav']         # Supported audio file extensions
TXT_EXTENSION = '.txt'                    # Transcript file extension
PROFILE_PREFIX = "profile-"               # Prefix for profile directory
PROFILE_SUFFIX = ".voicebox"              # Suffix for profile directory
ZIP_SUFFIX = ".voicebox.zip"              # Suffix for final zip file

# Regular Expression Patterns
NAME_PATTERN = r'^(.*?)(?:\s+(\d+))?$'    # Pattern to match voice name and number

# ============================================================================
# END OF CONFIGURATION
# ============================================================================

class VoiceBoxImporter:
    def __init__(self, api_base_url=API_BASE_URL):
        self.api_base_url = api_base_url
        self.profiles_url = f"{api_base_url}{PROFILES_ENDPOINT}"
        self.import_url = f"{api_base_url}{IMPORT_ENDPOINT}"
        
    def get_existing_profiles(self):
        """Fetch list of existing profiles from the API."""
        try:
            response = requests.get(self.profiles_url)
            response.raise_for_status()
            profiles = response.json()
            # Extract profile names
            return {profile.get('name', '').lower() for profile in profiles}
        except requests.exceptions.RequestException as e:
            print(f"Error fetching profiles: {e}")
            return set()
    
    def import_profile(self, zip_path):
        """Import a profile via the API."""
        try:
            with open(zip_path, 'rb') as f:
                files = {'file': (zip_path.name, f, 'application/zip')}
                response = requests.post(self.import_url, files=files)
                response.raise_for_status()
                return response.json()
        except requests.exceptions.RequestException as e:
            print(f"Error importing {zip_path.name}: {e}")
            return None
    
    def scan_and_import(self, voices_dir=VOICES_DIR, output_dir=OUTPUT_DIR, 
                       auto_import=AUTO_IMPORT, skip_existing=SKIP_EXISTING):
        """
        Scan the voices directory, create profiles, and optionally import them.
        
        Args:
            voices_dir: Path to the voices directory
            output_dir: Directory where profile zip files will be saved
            auto_import: If True, automatically import profiles
            skip_existing: If True, skip profiles that already exist
        """
        voices_path = Path(voices_dir)
        output_path = Path(output_dir)
        output_path.mkdir(exist_ok=True)
        
        # Get existing profiles if auto_import is enabled
        existing_profiles = set()
        if auto_import:
            print("Fetching existing profiles from API...")
            existing_profiles = self.get_existing_profiles()
            print(f"Found {len(existing_profiles)} existing profiles")
        
        # Group files by base name (without number suffix)
        voice_groups = defaultdict(list)
        
        # Pattern to match voice files with optional number suffix
        pattern = re.compile(NAME_PATTERN)
        
        # First pass: collect all voice files and their transcripts
        for file_path in voices_path.iterdir():
            if file_path.is_file() and file_path.suffix in WAV_EXTENSIONS:
                base_name = file_path.stem
                
                # Try to find matching .txt file
                txt_file = file_path.with_suffix(TXT_EXTENSION)
                if not txt_file.exists():
                    print(f"Warning: No transcript found for {file_path.name}")
                    continue
                
                # Read transcript
                with open(txt_file, 'r', encoding='utf-8') as f:
                    transcript = f.read().strip()
                
                # Parse name and number
                match = pattern.match(base_name)
                if match:
                    name = match.group(1).strip()
                    number = match.group(2)
                    if number is None:
                        # Check if this is "Name 1" format (with space but no number captured)
                        # or just "Name" (no number)
                        if ' ' in name and name.split(' ')[-1].isdigit():
                            # This handles cases like "Name 1" where the regex didn't capture properly
                            parts = name.rsplit(' ', 1)
                            name = parts[0]
                            number = parts[1]
                        else:
                            number = "1"
                else:
                    name = base_name
                    number = "1"
                
                # Store file info
                voice_groups[name].append({
                    'number': int(number),
                    'wav_path': file_path,
                    'txt_path': txt_file,
                    'transcript': transcript,
                    'original_name': base_name
                })
        
        # Process each voice group
        profiles_created = []
        profiles_imported = []
        profiles_skipped = []
        
        for voice_name, files in voice_groups.items():
            print(f"\nProcessing: {voice_name} ({len(files)} samples)")
            
            # Check if profile already exists
            profile_exists = voice_name.lower() in existing_profiles
            
            # Determine zip filename
            safe_name = voice_name.lower().replace(' ', '-')
            zip_filename = f"{PROFILE_PREFIX}{safe_name}{ZIP_SUFFIX}"
            zip_path = output_path / zip_filename
            
            # Create the profile package (always, so we have it ready)
            if self._create_profile_package(voice_name, files, output_path):
                profiles_created.append(zip_filename)
                
                # Check if we should import
                if auto_import:
                    if skip_existing and profile_exists:
                        print(f"  ⏭ Skipping import (already exists): {voice_name}")
                        profiles_skipped.append(voice_name)
                    else:
                        print(f"  Importing: {zip_filename}...")
                        result = self.import_profile(zip_path)
                        if result:
                            profiles_imported.append(voice_name)
                            print(f"  ✓ Successfully imported: {voice_name}")
                        else:
                            print(f"  ✗ Failed to import: {voice_name}")
                else:
                    print(f"  📦 Package created but not imported: {zip_filename}")
        
        # Print summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Profiles created: {len(profiles_created)}")
        print(f"Profiles imported: {len(profiles_imported)}")
        print(f"Profiles skipped (already exist): {len(profiles_skipped)}")
        
        if profiles_skipped:
            print("\nSkipped profiles:")
            for name in profiles_skipped:
                print(f"  - {name}")
        
        if profiles_imported:
            print("\nImported profiles:")
            for name in profiles_imported:
                print(f"  - {name}")
        
        print(f"\nProfile packages saved in: {output_path}")
        
        return {
            'created': profiles_created,
            'imported': profiles_imported,
            'skipped': profiles_skipped
        }
    
    def _create_profile_package(self, voice_name, files, output_path):
        """Create a VoiceBox profile package."""
        # Sort files by number
        files.sort(key=lambda x: x['number'])
        
        # Create temporary directory for profile
        safe_name = voice_name.lower().replace(' ', '-')
        temp_dir = output_path / f"{PROFILE_PREFIX}{safe_name}{PROFILE_SUFFIX}"
        temp_dir.mkdir(exist_ok=True)
        
        try:
            # Create samples directory
            samples_dir = temp_dir / "samples"
            samples_dir.mkdir(exist_ok=True)
            
            # Create manifest.json
            manifest = {
                "version": "1.0",
                "profile": {
                    "name": voice_name,
                    "description": "",
                    "language": "en"
                },
                "has_avatar": False
            }
            
            manifest_path = temp_dir / "manifest.json"
            with open(manifest_path, 'w', encoding='utf-8') as f:
                json.dump(manifest, f, indent=2)
            
            # Create samples.json and copy WAV files
            samples_data = {}
            
            for idx, file_info in enumerate(files):
                # Generate UUID for the sample
                sample_uuid = str(uuid.uuid4())
                wav_filename = f"{sample_uuid}.wav"
                
                # Copy WAV file to samples directory
                dest_wav = samples_dir / wav_filename
                shutil.copy2(file_info['wav_path'], dest_wav)
                
                # Add to samples.json
                samples_data[wav_filename] = file_info['transcript']
            
            samples_json_path = temp_dir / "samples.json"
            with open(samples_json_path, 'w', encoding='utf-8') as f:
                json.dump(samples_data, f, indent=2)
            
            # Create zip file
            zip_filename = f"{PROFILE_PREFIX}{safe_name}{ZIP_SUFFIX}"
            zip_path = output_path / zip_filename
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Add all files from temp directory
                for root, dirs, files_in_dir in os.walk(temp_dir):
                    for file in files_in_dir:
                        file_path = Path(root) / file
                        arcname = file_path.relative_to(temp_dir)
                        zipf.write(file_path, arcname)
            
            return True
            
        except Exception as e:
            print(f"Error creating profile for {voice_name}: {e}")
            return False
        finally:
            # Clean up temp directory
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

def main():
    # Get the current directory or specify paths
    script_dir = Path(__file__).parent
    voices_dir = script_dir / VOICES_DIR
    output_dir = script_dir / OUTPUT_DIR
    
    # Check if voices directory exists
    if not voices_dir.exists():
        print(f"Voices directory not found: {voices_dir}")
        print(f"Please make sure the '{VOICES_DIR}' directory exists in the current folder.")
        print("\nYou can change the VOICES_DIR constant at the top of the script.")
        return
    
    print(f"Scanning voices in: {voices_dir}")
    print(f"Output directory: {output_dir}")
    print(f"API Base URL: {API_BASE_URL}")
    print(f"Auto import: {AUTO_IMPORT}")
    print(f"Skip existing: {SKIP_EXISTING}")
    print("-" * 60)
    
    # Create importer and run
    importer = VoiceBoxImporter(API_BASE_URL)
    
    result = importer.scan_and_import(
        voices_dir=str(voices_dir),
        output_dir=str(output_dir),
        auto_import=AUTO_IMPORT,
        skip_existing=SKIP_EXISTING
    )
    
    print("\n" + "-" * 60)
    print("Done!")

if __name__ == "__main__":
    main()