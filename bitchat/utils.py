from .constants import DebugLevel, VERSION

DEBUG_LEVEL = DebugLevel.CLEAN


def debug_println(*args, **kwargs):
    if DEBUG_LEVEL >= DebugLevel.BASIC:
        print(*args, **kwargs)


def debug_full_println(*args, **kwargs):
    if DEBUG_LEVEL >= DebugLevel.FULL:
        print(*args, **kwargs)


def print_banner():
    print("\n\033[38;5;46m##\\       ##\\   ##\\               ##\\     ##\\")
    print("## |      \\__|  ## |              ## |                 ## |")
    print("#######\\  ##\\ ######\\    #######\\ #######\\   ######\\ ######\\")
    print("##  __##\\ ## |\\_##  _|  ##  _____|##  __##\\  \\____##\\\\_##  _|")
    print("## |  ## |## |  ## |    ## /      ## |  ## | ####### | ## |")
    print("## |  ## |## |  ## |##\\ ## |      ## |  ## |##  __## | ## |##\\")
    print("#######  |## |  \\####  |\\#######\\ ## |  ## |\\####### | \\####  |")
    print("\\_______/ \\__|   \\____/  \\_______|\\__|  \\__| \\_______|  \\____/")
    print("\n\033[38;5;40m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m")
    print("\033[37mDecentralized • Encrypted • Peer-to-Peer • Open Source\033[0m")
    print(f"\033[37m                bitchat@ the terminal {VERSION}\033[0m")
    print("\033[38;5;40m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n")

__all__ = ['DEBUG_LEVEL', 'debug_println', 'debug_full_println', 'print_banner']
