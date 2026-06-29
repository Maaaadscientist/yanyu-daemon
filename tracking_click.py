import ast
import time
import os
from datetime import datetime

from automation import GameAutomation, calculate_event_time
from coordinates import *

log_file_path = "event_log.txt"

def log_event(event_name, execution_time, remaining_time):
    with open(log_file_path, "a") as log_file:
        log_file.write(f"{event_name}: {execution_time}, Remaining Time: {remaining_time}\n")

def read_log():
    event_times = {}
    default_values = {
        "2_hour": 0,
        "3_hour": 0,
        "5_hour": 0,
        "6_hour": 0,
    }
    
    if os.path.exists(log_file_path):
        with open(log_file_path, "r") as log_file:
            for line in log_file:
                # Split on "Last Execution" and "Remaining Time"
                parts = line.strip().split(", Remaining Time: ")
                if len(parts) == 2:
                    execution_time_str = parts[0].split("Last Execution: ")[1].strip()
                    remaining_info = parts[1].strip()
                    
                    # Parse the remaining time dictionary
                    try:
                        remaining_time = ast.literal_eval(remaining_info)
                        
                        # Calculate the time difference
                        execution_time = datetime.strptime(execution_time_str, '%Y-%m-%d %H:%M:%S.%f')
                        time_diff = (datetime.now() - execution_time).total_seconds() / 60  # Convert to minutes
                        
                        # Adjust remaining times based on the time difference
                        for key, value in remaining_time.items():
                            new_remaining_time = value - time_diff
                            event_times[key] = max(0, new_remaining_time)  # Ensure non-negative
                        
                    except Exception as e:
                        print(f"Error parsing line: {line}. Exception: {e}")
    else:
        # If the log file doesn't exist, use default values
        event_times.update(default_values)

    return event_times
def main():
    automation = GameAutomation()
    time.sleep(3)

    def auto_click_event(click_list):
        automation.run_actions(click_list)
    
    current_time = datetime.now()
    # Load previously logged remaining times
    event_remainings = read_log()

    print(event_remainings)
    #min_1hour = 0  + ( ( event_remainings.get("1_hour", 0) - 2 ) // 60 + 1 ) * 60  # Add remaining time
    #min_2hour = 11 + ( ( event_remainings.get("2_hour", 0) - 2 ) // 60 + 1 ) * 60  # Add remaining time
    #min_3hour = 14 + ( ( event_remainings.get("3_hour", 0) - 2 ) // 60 + 1 ) * 60
    #min_5hour = 22 + ( ( event_remainings.get("5_hour", 0) - 2 ) // 60 + 1 ) * 60
    #min_6hour = 20 + ( ( event_remainings.get("6_hour", 0) - 2 ) // 60 + 1 ) * 60
    if event_remainings.get("1_hour", 0) < 1 and event_remainings.get("2_hour", 0) < 1 and  event_remainings.get("3_hour", 0) < 1:
        min_1hour = 1     # Add remaining time
        min_2hour = 11    # Add remaining time
        min_3hour = 15  
        min_5hour = 24  
        min_6hour = 21  
    else:
        min_1hour = event_remainings.get("1_hour", 0) + 2  # Add remaining time
        min_2hour = event_remainings.get("2_hour", 0) + 2  # Add remaining time
        if min_2hour % 60 < min_1hour + 11:
            min_2hour = min_1hour + 11 + (min_2hour // 60 ) * 62
        min_3hour = event_remainings.get("3_hour", 0) + 2
        if min_3hour % 60 < min_1hour + 14:
            min_3hour = min_1hour + 14 + (min_3hour // 60 ) * 62
        min_5hour = event_remainings.get("5_hour", 0) + 2
        min_6hour = event_remainings.get("6_hour", 0) + 2
        if min_6hour % 60 < min_1hour + 20:
            min_6hour = min_1hour + 20 + (min_6hour // 60 ) * 62
        if min_5hour % 60 < min_1hour + 22:
            min_5hour = min_1hour + 22 + (min_6hour // 60 ) * 62 
        
    intervals = {
        "1_hour": 63,
        "2_hour": 122,
        "3_hour": 182,
        "5_hour": 302,
        "6_hour": 362,
        "daily": 1442
    }
    
    event_times = {
        "1_hour": calculate_event_time(current_time, min_1hour),
        "2_hour": calculate_event_time(current_time, min_2hour),
        "3_hour": calculate_event_time(current_time, min_3hour),
        "5_hour": calculate_event_time(current_time, min_5hour),
        "6_hour": calculate_event_time(current_time, min_6hour),
        "daily": calculate_event_time(current_time, 0)
    }
    print(event_times)
    
    second_lag = 30
    
    def schedule_events(event_times, second_lag):
        current_time = datetime.now()
    
        for name, event_time in event_times.items():
            if current_time >= event_time and current_time.second > second_lag:
                print(f"{name} event at {current_time}")
                # Execute events here
                # (similar logic to your previous code)

                if name == "1_hour":
                    auto_click_event(bear1)
                    auto_click_event(bear_tianshan)
                    auto_click_event(bear2)
                    auto_click_event(bear3)
                    auto_click_event(bear4)
                    auto_click_event(bear5)
                    auto_click_event(bear6)
                    auto_click_event(bear8)
                    auto_click_event(bear9)
                    auto_click_event(bear10)
                    auto_click_event(bear11)
                    auto_click_event(bear12)
                    auto_click_event(bear13)
                    auto_click_event(bear15)
                    auto_click_event(pig2)
                    auto_click_event(pig1)
                    auto_click_event(save)
                elif name == "2_hour":
                    auto_click_event(sleep1)
                    auto_click_event(xigua)
                    auto_click_event(jiazhai)
                    auto_click_event(save)
                elif name == "3_hour":
                    auto_click_event(sleep1)
                    auto_click_event(bear7)
                    auto_click_event(cow2)
                    auto_click_event(cow1)
                    auto_click_event(bear14)
                    auto_click_event(save)
                elif name == "5_hour":
                    auto_click_event(xiangjiao)
                    auto_click_event(shanzha)
                    auto_click_event(pingguo)
                    auto_click_event(changbaipingguo)
                    auto_click_event(lianou)
                    auto_click_event(save)
                elif name == "6_hour":
                    auto_click_event(jianshui)
                    auto_click_event(hexia1)
                    auto_click_event(hexia2)
                    auto_click_event(save)
                elif name == "daily":
                    auto_click_event(sleep2)
                    auto_click_event(suancai)
                    #auto_click_event(luoyangrichang1)
                    #auto_click_event(diling1)
                    #auto_click_event(hangzhouxiuwei)
                    #auto_click_event(xiaotili)
                    #auto_click_event(luoyangrichang2)
                    #auto_click_event(wenxiangjiao)
                    #auto_click_event(jujingyunbiao)
                    #auto_click_event(luoyangrichang3)
                    #auto_click_event(wushendian)
                    #auto_click_event(kuileixiuwei)
    
                # Reschedule event after its interval has passed
                event_times[name] = calculate_event_time(current_time, intervals[name] - second_lag)
                # Calculate the next execution time based on the original interval, ignoring execution time
                next_event_time = event_time + timedelta(minutes=intervals[name])
                event_times[name] = next_event_time
    
    # Main loop
    try:
        print("preparing for executing the script:")
        time.sleep(5)
        while True:
            schedule_events(event_times, second_lag)
            time.sleep(1)
    except KeyboardInterrupt:
        # Log the last time when quitting the script
        last_time = datetime.now()
        remaining_times = {name: (event_times[name] -last_time).seconds // 60  + (event_times[name] -last_time).days * 1440 for name in intervals}
        log_event("Last Execution", last_time, remaining_times)
        print("Script terminated. Last execution logged.")

if __name__ == "__main__":
    main()
