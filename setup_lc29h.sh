#!/bin/bash
#
# configure_gps.sh: script to provide GPS modules with commands
# that are not saved in flash on the module (ie. they must be provided
# each time the module is started).

BASEDIR="$(dirname "$0")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for better output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

LC29HBS_REQUIRED_FILES=(
    "LC29HBS_Factory_Defaults.txt"
    "LC29HBS_Configure.txt"
    "LC29HBS_Save.txt"
)

# Function for colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Build a full NMEA sentence by appending checksum when missing
build_nmea_sentence() {
    local sentence="$1"
    local payload checksum i c ord

    if [[ "${sentence}" == *\** ]]; then
        printf '%s' "${sentence}"
        return 0
    fi

    payload="${sentence#\$}"
    checksum=0
    for ((i=0; i<${#payload}; i++)); do
        c="${payload:i:1}"
        printf -v ord '%d' "'${c}"
        ((checksum ^= ord))
    done

    printf '$%s*%02X' "${payload}" "${checksum}"
}

# Validate required LC29HBS command files are present
validate_lc29hbs_files() {
    local cfg_dir="${BASEDIR}/receiver_cfg"
    local missing=()
    local f

    for f in "${LC29HBS_REQUIRED_FILES[@]}"; do
        [[ -f "${cfg_dir}/${f}" ]] || missing+=("${f}")
    done

    if [[ ${#missing[@]} -gt 0 ]]; then
        print_error "Missing LC29HBS command files in ${cfg_dir}"
        for f in "${missing[@]}"; do
            print_error "  - ${f}"
        done
        print_warning "Add the missing files and run the script again."
        return 1
    fi

    return 0
}

# Installation and setup
setup_installation() {
    print_status "Checking installation..."

    mkdir -p "${BASEDIR}/receiver_cfg" "${BASEDIR}/tools"
    
    # Check if receiver_cfg folder and files exist
    if [[ ! -f "${BASEDIR}/receiver_cfg/LC29HBS_Configure.txt" ]]; then
        print_warning "receiver_cfg files not found. Copying files..."
                
        # Copy files from the current script directory
        if [[ "${SCRIPT_DIR}" != "${BASEDIR}" && -d "${SCRIPT_DIR}/receiver_cfg" ]]; then
            cp -r "${SCRIPT_DIR}/receiver_cfg"/* "${BASEDIR}/receiver_cfg/"
            print_status "receiver_cfg files copied"
        else
            print_warning "No alternate receiver_cfg source found in ${SCRIPT_DIR}"
        fi
        
        if [[ "${SCRIPT_DIR}" != "${BASEDIR}" && -d "${SCRIPT_DIR}/tools" ]]; then
            cp -r "${SCRIPT_DIR}/tools"/* "${BASEDIR}/tools/"
            print_status "tools files copied"
        else
            print_warning "No alternate tools source found in ${SCRIPT_DIR}"
        fi
    else
        print_status "receiver_cfg files already present"
    fi
    
    # Check and create settings.conf
    if [[ ! -f "${BASEDIR}/settings.conf" ]]; then
        if [[ -f "${BASEDIR}/settings.conf.default" ]]; then
            cp "${BASEDIR}/settings.conf.default" "${BASEDIR}/settings.conf"
            print_status "settings.conf created from default"
        else
            print_error "settings.conf.default not found"
            return 1
        fi
    fi
    
    # Configure settings.conf
    configure_settings

    # Final validation for LC29HBS mode
    validate_lc29hbs_files || return 1
    
    return 0
}

# Configure settings
configure_settings() {
    print_status "Configuring settings.conf..."
    
    echo -e "\n${BLUE}Select your hardware configuration:${NC}"
    echo "1) Waveshare HAT (ttyS0, 115200 baud)"
    echo "2) Custom"
    
    read -p "Your choice (1-2): " hw_choice
    
    case $hw_choice in
        1)
            # Waveshare configuration
            sed -i 's/^com_port=.*/com_port=ttyS0/' "${BASEDIR}/settings.conf"
            sed -i 's/^com_port_settings=.*/com_port_settings=115200:8:n:1/' "${BASEDIR}/settings.conf"
            sed -i 's/^receiver=.*/receiver="Quectel LC29HBS"/' "${BASEDIR}/settings.conf"
            print_status "Waveshare configuration applied"
            ;;
        2)
            # Custom configuration
            read -p "COM Port (e.g. ttyUSB0): " custom_port
            read -p "Baud rate (e.g. 115200): " custom_baud
            
            sed -i "s/^com_port=.*/com_port=${custom_port}/" "${BASEDIR}/settings.conf"
            sed -i "s/^com_port_settings=.*/com_port_settings=${custom_baud}:8:n:1/" "${BASEDIR}/settings.conf"
            sed -i 's/^receiver=.*/receiver="Quectel LC29HBS"/' "${BASEDIR}/settings.conf"
            print_status "Custom configuration applied"
            ;;
        *)
            print_warning "Invalid selection, using default values"
            ;;
    esac
}

# Load selected keys from settings.conf safely (supports values with spaces)
load_settings() {
    local key value

    com_port=""
    com_port_settings=""
    receiver=""

    while IFS='=' read -r key value; do
        # Skip comments and empty lines
        [[ -z "${key}" || "${key}" =~ ^[[:space:]]*# ]] && continue

        # Trim spaces around key and value
        key="${key#${key%%[![:space:]]*}}"
        key="${key%${key##*[![:space:]]}}"
        value="${value#${value%%[![:space:]]*}}"
        value="${value%${value##*[![:space:]]}}"

        # Remove optional surrounding quotes
        if [[ "${value}" =~ ^\".*\"$ ]]; then
            value="${value:1:${#value}-2}"
        fi

        case "${key}" in
            com_port|com_port_settings|receiver)
                printf -v "${key}" '%s' "${value}"
                ;;
        esac
    done < "${BASEDIR}/settings.conf"
}

# Execute NMEA command
execute_nmea_command() {
    local file="$1"
    local description="$2"
    local cfg_path="${BASEDIR}/receiver_cfg/${file}"
    local device="/dev/${com_port}"
    local line trimmed sentence
    
    if [[ ! -f "${cfg_path}" ]]; then
        print_error "File ${file} not found"
        return 1
    fi

    if [[ -z "${com_port}" || -z "${speed}" ]]; then
        print_error "Serial settings are missing (com_port='${com_port}', speed='${speed}')"
        return 1
    fi

    if [[ ! -e "${device}" ]]; then
        print_error "Serial device ${device} not found"
        return 1
    fi

    if ! stty -F "${device}" "${speed}" cs8 -cstopb -parenb -ixon -ixoff -icanon -echo min 1 time 1; then
        print_error "Unable to configure serial device ${device} at ${speed} baud"
        return 1
    fi
    
    print_status "Executing: ${description}"

    while IFS= read -r line || [[ -n "${line}" ]]; do
        line="${line%$'\r'}"

        # Trim leading/trailing whitespace
        trimmed="${line#${line%%[![:space:]]*}}"
        trimmed="${trimmed%${trimmed##*[![:space:]]}}"

        [[ -z "${trimmed}" || "${trimmed}" == \#* ]] && continue

        if [[ "${trimmed}" != \$* ]]; then
            print_warning "Skipping invalid command line: ${trimmed}"
            continue
        fi

        sentence="$(build_nmea_sentence "${trimmed}")"

        if ! printf '%s\r\n' "${sentence}" > "${device}"; then
            print_error "Failed to send command '${sentence}' to ${device}"
            return 1
        fi

        # Allow receiver to process sequential commands
        sleep 0.2
    done < "${cfg_path}"
    
    if [[ $? -eq 0 ]]; then
        print_status "${description} executed successfully"
        return 0
    else
        print_error "${description} failed"
        return 1
    fi
}

# Menu for LC29HBS configuration
show_lc29hbs_menu() {
    validate_lc29hbs_files || return 1

    while true; do
        echo -e "\n${BLUE}=== Quectel LC29HBS Configuration ===${NC}"
        echo "Port: /dev/${com_port} | Speed: ${speed}"
        echo ""
        echo "1) Run all steps (Factory Defaults -> Configure -> Save)"
        echo "2) Configure and Save only"
        echo "3) Factory Defaults only"
        echo "4) Configure only"
        echo "5) Save only"
        echo "6) Back to main menu"
        echo ""
        read -p "Your choice (1-6): " choice
        
        case $choice in
            1)
                print_status "Running all steps..."
                execute_nmea_command "LC29HBS_Factory_Defaults.txt" "Factory Defaults"
                sleep 2
                execute_nmea_command "LC29HBS_Configure.txt" "Configuration"
                sleep 2
                execute_nmea_command "LC29HBS_Save.txt" "Save Configuration"
                ;;
            2)
                print_status "Running Configure and Save..."
                execute_nmea_command "LC29HBS_Configure.txt" "Configuration"
                sleep 2
                execute_nmea_command "LC29HBS_Save.txt" "Save Configuration"
                ;;
            3)
                execute_nmea_command "LC29HBS_Factory_Defaults.txt" "Factory Defaults"
                ;;
            4)
                execute_nmea_command "LC29HBS_Configure.txt" "Configuration"
                ;;
            5)
                execute_nmea_command "LC29HBS_Save.txt" "Save Configuration"
                ;;
            6)
                break
                ;;
            *)
                print_warning "Invalid selection"
                ;;
        esac
        
        echo ""
        read -p "Press Enter to continue..."
    done
}

# Main menu
show_main_menu() {
    while true; do
        echo -e "\n${BLUE}=== GPS Configuration Tool ===${NC}"
        echo "1) Run installation/setup"
        echo "2) Reconfigure settings"
        echo "3) Configure GPS module"
        echo "4) Exit"
        echo ""
        read -p "Your choice (1-4): " main_choice
        
        case $main_choice in
            1)
                setup_installation
                ;;
            2)
                configure_settings
                ;;
            3)
                # Load settings
                if [[ -f "${BASEDIR}/settings.conf" ]]; then
                    load_settings
                    
                    if [[ "${receiver}" = "Quectel LC29HBS" ]]; then
                        speed="${com_port_settings%%:*}"
                        show_lc29hbs_menu
                    else
                        print_error "Receiver '${receiver}' is not supported"
                    fi
                else
                    print_error "settings.conf not found. Please run installation first."
                fi
                ;;
            4)
                print_status "Program exited"
                exit 0
                ;;
            *)
                print_warning "Invalid selection"
                ;;
        esac
    done
}

# Main program
main() {
    echo -e "${GREEN}GPS Configuration Tool started${NC}"
    
    # Check if Python3 is available
    if ! command -v python3 &> /dev/null; then
        print_error "Python3 is not installed"
        exit 1
    fi
    
    show_main_menu
}

# Start script
main "$@"
