from typing import Dict, List


SYSTEM_FOLDERS = {
    "Inbox", "Sent", "Trash", "Drafts",
    "Junk", "Calendar", "Contacts",
    "Task", "Briefcase", "Chats",
    "Emailed Contacts", "USER_ROOT"
}


def build_account_profile(real_data: Dict) -> Dict:
    
    folders = real_data.get("folders",[])
    filters = real_data.get("filters", [])
    contacts = real_data.get("contacts", [])
    signature = real_data.get("signature", "")
    last_sent = real_data.get("last_sent_subjects", [])
    
    
    custom_folders = [
        f for f in folders
        if f not in SYSTEM_FOLDERS
    ]
    
    return {
        "has_custom_folders": bool(custom_folders),
        "has_filters": bool(filters),
        "has_contacts": bool(contacts),
        "has_signature": bool(signature),
        "has_sent": bool(last_sent)
    }
    
    
def generate_adaptive_questions(profile: Dict, question_bank: Dict) -> List[str]:
    possible = []
    
    for key, config in question_bank.items():
        
        q_type = config.get("type")
        
        if q_type == "folders" and profile["has_custom_folders"]:
            possible.append(key)
            
        if q_type == "filters" and profile["has_filters"]:
            possible.append(key)
            
        if q_type == "contacts" and profile["has_contacts"]:
            possible.append(key)
            
        if q_type == "signature" and profile["has_signature"]:
            possible.append(key)
            
        if q_type == "last_sent_subjects" and profile["has_sent"]:
            possible.append(key)
            
    return possible