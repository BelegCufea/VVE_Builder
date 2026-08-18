"""
Infinity Engine Voice Sample Auditor

A Streamlit-based application for auditing and managing voice sample files
for Infinity Engine game mods. This tool helps organize voice samples by NPC,
edit their associated text files, and move approved samples to the final
voices directory.

Usage:
    streamlit run profiles-audit.py

Note: This application must be run using Streamlit. Double-clicking the file
or using 'py profiles-audit.py' will not work because Streamlit provides
the necessary web server and UI framework.
"""

import streamlit as st
from pathlib import Path
import shutil
import re
import json
from typing import Dict, List, Set, Optional
import pyperclip

# ============================================================================
# Configuration Constants
# ============================================================================

VOICES_PREP_DIR = "voices_prep"          # Directory containing raw voice samples
VOICES_DIR = "voices"                    # Directory for approved voice samples
SKIPPED_CONFIG_PATH = "profiles-audit-skipped.json"  # Persistent skip list storage

# Initialize path objects for file operations
voices_prep_dir = Path(VOICES_PREP_DIR)
voices_dir = Path(VOICES_DIR)

# ============================================================================
# Streamlit Page Configuration
# ============================================================================

st.set_page_config(
    page_title="Infinity Engine Voice Auditor",
    page_icon="🎙️",
    layout="wide"
)

# ============================================================================
# Persistent Storage Functions
# ============================================================================

def load_skipped_npcs() -> Set[str]:
    """
    Load the set of skipped NPC names from the JSON configuration file.
    
    Returns:
        Set[str]: A set of NPC names that have been marked as skipped.
                 Returns an empty set if the file doesn't exist or is corrupted.
    
    The skipped NPCs are stored persistently so that the skip status survives
    application restarts.
    """
    path = Path(SKIPPED_CONFIG_PATH)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            # If the file is corrupted or unreadable, return an empty set
            return set()
    return set()


def save_skipped_npcs(skipped_set: Set[str]) -> None:
    """
    Save the set of skipped NPC names to the JSON configuration file.
    
    Args:
        skipped_set: A set of NPC names to be persisted as skipped.
    
    This function writes the skipped NPC list to a JSON file to maintain
    state across application sessions.
    """
    path = Path(SKIPPED_CONFIG_PATH)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(skipped_set), f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving skipped NPCs: {e}")


# ============================================================================
# Session State Initialization
# ============================================================================

# Initialize session state with skipped NPCs loaded from the JSON file
if "skipped_npcs" not in st.session_state:
    st.session_state.skipped_npcs = load_skipped_npcs()

# Initialize session state for mode selection
if "audit_mode" not in st.session_state:
    st.session_state.audit_mode = "prep"  # "prep" or "approved"


def load_npc_groups() -> Dict[str, List[Dict]]:
    """
    Scan the voices_prep directory and group files by NPC name.
    
    Returns:
        Dict[str, List[Dict]]: A dictionary where:
            - Key: NPC name (string)
            - Value: List of sample dictionaries, each containing:
                - 'sample_num': The sample number (int, defaults to 1 if not present)
                - 'wav_path': Path to the WAV file
                - 'txt_path': Path to the TXT file
                - 'stem': The base filename without extension
    
    The function looks for WAV files and pairs them with corresponding TXT files.
    NPC names are derived from the filename pattern: "NPCNAME [number]" where
    the number is optional and defaults to 1 if not present.
    
    Example filename patterns:
        - "NPCNAME.WAV" -> sample_num = 1
        - "NPCNAME 2.WAV" -> sample_num = 2
        - "NPCNAME 3.WAV" -> sample_num = 3
    """
    npcs = {}
    
    # Check if the source directory exists
    if not voices_prep_dir.exists():
        return npcs
    
    # Iterate through all WAV files in the preparation directory
    for wav_path in voices_prep_dir.glob("*.WAV"):
        stem = wav_path.stem
        
        # Extract NPC name and sample number using regex
        # Pattern: Capture everything before an optional space and number at the end
        match = re.match(r'^(.*?)(?:\s+(\d+))?$', stem)
        if match:
            npc_name = match.group(1).strip()
            idx = match.group(2)
            sample_num = int(idx) if idx else 1
            
            # Construct the path to the corresponding TXT file
            txt_path = wav_path.with_suffix('.txt')
            
            # Initialize the NPC entry if it doesn't exist
            if npc_name not in npcs:
                npcs[npc_name] = []
            
            # Add the sample to the NPC's list
            npcs[npc_name].append({
                "sample_num": sample_num,
                "wav_path": wav_path,
                "txt_path": txt_path,
                "stem": stem
            })
    
    # Sort samples by sample number for each NPC
    for npc in npcs:
        npcs[npc].sort(key=lambda x: x["sample_num"])
    
    # Return a sorted dictionary by NPC name for consistent display
    return dict(sorted(npcs.items()))


def load_approved_npc_groups() -> Dict[str, List[Dict]]:
    """
    Scan the voices directory and group files by NPC name (for already approved voices).
    
    Returns:
        Dict[str, List[Dict]]: Same structure as load_npc_groups() but from VOICES_DIR
    
    This is identical to load_npc_groups() but reads from the approved voices directory
    instead of the preparation directory.
    """
    npcs = {}
    
    # Check if the voices directory exists
    if not voices_dir.exists():
        return npcs
    
    # Iterate through all WAV files in the voices directory
    for wav_path in voices_dir.glob("*.WAV"):
        stem = wav_path.stem
        
        # Extract NPC name and sample number using regex
        match = re.match(r'^(.*?)(?:\s+(\d+))?$', stem)
        if match:
            npc_name = match.group(1).strip()
            idx = match.group(2)
            sample_num = int(idx) if idx else 1
            
            # Construct the path to the corresponding TXT file
            txt_path = wav_path.with_suffix('.txt')
            
            # Initialize the NPC entry if it doesn't exist
            if npc_name not in npcs:
                npcs[npc_name] = []
            
            # Add the sample to the NPC's list
            npcs[npc_name].append({
                "sample_num": sample_num,
                "wav_path": wav_path,
                "txt_path": txt_path,
                "stem": stem
            })
    
    # Sort samples by sample number for each NPC
    for npc in npcs:
        npcs[npc].sort(key=lambda x: x["sample_num"])
    
    # Return a sorted dictionary by NPC name for consistent display
    return dict(sorted(npcs.items()))


# ============================================================================
# Main Application Logic
# ============================================================================

# Load NPC data based on current mode
if st.session_state.audit_mode == "prep":
    npcs = load_npc_groups()
    mode_icon = "📝"
    mode_title = "Review New Voices"
else:
    npcs = load_approved_npc_groups()
    mode_icon = "🔍"
    mode_title = "Review Approved Voices"

total_npcs_count = len(npcs)

# --- SIDEBAR: Overview, Filters & Mode ---
st.sidebar.header("📊 Overview & Filters")

# Mode selection
st.sidebar.markdown("### 🔄 Mode")
current_mode = st.sidebar.radio(
    "Select mode:",
    options=["📝 Review New Voices", "🔍 Review Approved Voices"],
    index=0 if st.session_state.audit_mode == "prep" else 1,
    label_visibility="collapsed"
)

# Update mode based on selection
new_mode = "prep" if "📝" in current_mode else "approved"
if new_mode != st.session_state.audit_mode:
    st.session_state.audit_mode = new_mode
    st.rerun()

# Display current mode status
if st.session_state.audit_mode == "prep":
    st.sidebar.info("🔄 New voices from `voices_prep/`")
else:
    st.sidebar.info("🔄 Approved voices from `voices/`")

st.sidebar.markdown("---")

# Statistics - compact display
col1, col2 = st.sidebar.columns(2)
with col1:
    st.metric("Total", total_npcs_count)
with col2:
    if st.session_state.audit_mode == "prep":
        skipped_count = len(st.session_state.skipped_npcs)
        st.metric("Skipped", skipped_count)

# ============================================================================
# MAIN AREA - Split Layout (NPC List | Samples)
# ============================================================================

# Title
if st.session_state.audit_mode == "prep":
    st.title("📝 Infinity Engine Voice Auditor - Review New Voices")
else:
    st.title("🔍 Infinity Engine Voice Auditor - Review Approved Voices")

# Create two columns: left for NPC list (30%), right for samples (70%)
left_col, right_col = st.columns([0.2, 0.8], gap="medium")

# ============================================================================
# LEFT COLUMN: NPC List
# ============================================================================

with left_col:
    # Title row with checkbox
    title_col, checkbox_col = st.columns([2, 1])
    with title_col:
        st.markdown("#### 📋 NPC List")
    with checkbox_col:
        hide_skipped = st.checkbox("Hide Skipped", value=True, key="hide_skipped_main")
    
    # Search/filter input
    search_term = st.text_input("🔍 Filter NPCs", placeholder="Type to filter...", key="npc_search")    
    # Filter visible NPCs based on skip status and search
    visible_npcs = {}
    for name, samples in npcs.items():
        is_skipped = name in st.session_state.skipped_npcs
        
        # Apply hide skipped filter
        if hide_skipped and is_skipped:
            continue
        
        # Apply search filter
        if search_term and search_term.lower() not in name.lower():
            continue
            
        visible_npcs[name] = samples
    
    visible_npcs_count = len(visible_npcs)
    
    # Show count of visible NPCs
    st.caption(f"Showing {visible_npcs_count} of {total_npcs_count} NPCs")
    
    if not visible_npcs:
        st.info("No NPCs match your filters.")
        st.stop()
    
    # Build display labels with icons
    npc_labels = {}
    for name in visible_npcs.keys():
        is_skipped = name in st.session_state.skipped_npcs
        icon = "⏭️ " if is_skipped else "👤 "
        npc_labels[f"{icon}{name}"] = name
    
    # Manage selection state
    if "selected_npc" not in st.session_state or st.session_state.selected_npc not in visible_npcs:
        st.session_state.selected_npc = list(visible_npcs.keys())[0]
    
    # Find the index of the currently selected NPC
    npc_list = list(npc_labels.keys())
    default_index = 0
    if st.session_state.selected_npc in list(visible_npcs.keys()):
        # Find the label that corresponds to the selected NPC
        for i, label in enumerate(npc_list):
            if npc_labels[label] == st.session_state.selected_npc:
                default_index = i
                break
    
    # Create the radio button list for NPC selection
    selected_label = st.radio(
        "Select NPC:",
        options=npc_list,
        index=default_index,
        label_visibility="collapsed",
        key="npc_radio"
    )
    
    selected_npc = npc_labels.get(selected_label)
    st.session_state.selected_npc = selected_npc

# ============================================================================
# RIGHT COLUMN: Sample Editor
# ============================================================================

with right_col:
    # Check if an NPC is selected
    if not selected_npc:
        st.info("ℹ️ No NPC selected.")
        st.stop()
    
    # Get the samples for the selected NPC
    samples = visible_npcs[selected_npc]
    is_skipped = selected_npc in st.session_state.skipped_npcs
    
    # Header with NPC info and actions
    with st.container(border=True):
        col_title, col_copy, col_skip = st.columns([3, 1, 1])
        
        with col_title:
            if st.session_state.audit_mode == "prep":
                st.subheader(f"📝 {selected_npc}")
            else:
                st.subheader(f"🔍 {selected_npc}")
            st.caption(f"Total samples: {len(samples)}")
        
        with col_copy:
            # Copy NPC name to clipboard button
            if st.button("📋 Copy Name", key=f"copy_{selected_npc}", use_container_width=True):
                pyperclip.copy(selected_npc)
                st.toast(f"Copied '{selected_npc}' to clipboard!", icon="✅")
        
        with col_skip:
            # Skip checkbox that persists the skip status (only in prep mode)
            if st.session_state.audit_mode == "prep":
                skip_status = st.checkbox("Skip this NPC", value=is_skipped, key=f"skip_{selected_npc}")
                
                # Handle skip status changes
                if skip_status and selected_npc not in st.session_state.skipped_npcs:
                    st.session_state.skipped_npcs.add(selected_npc)
                    save_skipped_npcs(st.session_state.skipped_npcs)
                    st.rerun()
                elif not skip_status and selected_npc in st.session_state.skipped_npcs:
                    st.session_state.skipped_npcs.remove(selected_npc)
                    save_skipped_npcs(st.session_state.skipped_npcs)
                    st.rerun()
    
    # Sample list
    st.markdown("### Samples")
    
    # Loop through each sample for the selected NPC
    for i, sample in enumerate(samples):
        st.markdown(f"**Sample #{sample['sample_num']}** (`{sample['stem']}`)")
        
        # Read existing text content
        text_content = ""
        if sample['txt_path'].exists():
            text_content = sample['txt_path'].read_text(encoding='utf-8')
        
        # Text input area for editing the sample's text
        new_text = st.text_area(
            f"Edit text for {sample['stem']}",
            value=text_content,
            key=f"text_{selected_npc}_{i}",
            height=110
        )
        
        # Save the text if it was modified
        if new_text != text_content:
            sample['txt_path'].write_text(new_text, encoding='utf-8')
        
        # Audio player for the WAV file
        if sample['wav_path'].exists():
            st.audio(str(sample['wav_path']))
        else:
            st.error(f"Audio file missing: {sample['wav_path'].name}")
        
        # Add a separator between samples except after the last one
        if i < len(samples) - 1:
            st.markdown("---")
    
    st.markdown("---")
    
    # --- Action Buttons ---
    col_approve, col_pad = st.columns([1, 3])
    
    with col_approve:
        # Different button behavior based on mode
        if st.session_state.audit_mode == "prep":
            # Approve button for new voices - move to voices directory
            if st.button("✅ Approve & Move to Voices", type="primary", use_container_width=True):
                # Create the destination directory if it doesn't exist
                voices_dir.mkdir(parents=True, exist_ok=True)
                
                moved_count = 0
                
                # Move all files for this NPC to the voices directory
                for sample in samples:
                    if sample['wav_path'].exists():
                        shutil.move(str(sample['wav_path']), str(voices_dir / sample['wav_path'].name))
                    if sample['txt_path'].exists():
                        shutil.move(str(sample['txt_path']), str(voices_dir / sample['txt_path'].name))
                    moved_count += 1
                
                # If the NPC was skipped, remove it from the skipped list after approval
                if selected_npc in st.session_state.skipped_npcs:
                    st.session_state.skipped_npcs.remove(selected_npc)
                    save_skipped_npcs(st.session_state.skipped_npcs)
                
                st.success(f"Successfully moved {moved_count} files for `{selected_npc}` to `{voices_dir}`!")
                st.rerun()
        else:
            # Unapprove button for approved voices - move back to prep directory
            if st.button("↩️ Unapprove & Move Back", type="primary", use_container_width=True):
                # Create the prep directory if it doesn't exist
                voices_prep_dir.mkdir(parents=True, exist_ok=True)
                
                moved_count = 0
                
                # Move all files for this NPC back to the prep directory
                for sample in samples:
                    if sample['wav_path'].exists():
                        shutil.move(str(sample['wav_path']), str(voices_prep_dir / sample['wav_path'].name))
                    if sample['txt_path'].exists():
                        shutil.move(str(sample['txt_path']), str(voices_prep_dir / sample['txt_path'].name))
                    moved_count += 1
                
                st.success(f"Successfully moved {moved_count} files for `{selected_npc}` back to `{voices_prep_dir}`!")
                st.rerun()