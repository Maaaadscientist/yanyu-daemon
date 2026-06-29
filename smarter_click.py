import time
import sys
from datetime import datetime

from automation import GameAutomation, calculate_event_time
from coordinates import *

def main():
    automation = GameAutomation(position_names={v: k for k, v in pos.items()})
    time.sleep(3)

    def auto_click_event(click_list):
        automation.run_actions(click_list)
    
    # Get the current time and compensate if needed
    current_time = datetime.now()
    compensate = 0
    hour_lag = 0
    
    if len(sys.argv) == 2:
        compensate = int(sys.argv[1])
    elif len(sys.argv) == 3:
        hour_lag = int(sys.argv[2])
    current_minute = current_time.minute
    current_hour = current_time.hour
    
    # Set up initial event offsets
    min_1hour =   compensate
    min_2hour =   11 + compensate + 60 * hour_lag
    min_3hour =   14 + compensate + 60 * hour_lag
    min_5hour =   22 + compensate
    min_6hour =   20 + compensate
    min_day =   25 + compensate
    
    # Set up event intervals (in minutes)
    intervals = {
        "1_hour": 62,
        "2_hour": 122,
        "3_hour": 182,
        "5_hour": 302,
        "6_hour": 362,
        "daily": 1442  # 24 hours + compensation
    }
    
    # Calculate the initial execution times
    event_times = {
        "1_hour": calculate_event_time(current_time, min_1hour),
        "2_hour": calculate_event_time(current_time, min_2hour),
        "3_hour": calculate_event_time(current_time, min_3hour),
        "5_hour": calculate_event_time(current_time, min_5hour),
        "6_hour": calculate_event_time(current_time, min_6hour),
        "daily": calculate_event_time(current_time, min_day),
    }
    
    print(event_times)
    second_lag = 30
        
    def schedule_events(event_times, second_lag):
        current_time = datetime.now()
    
        # Check and execute each event based on its interval
        for name, event_time in event_times.items():
            if current_time >= event_time and current_time.second > second_lag:
                print(f"{name} event at {current_time}")
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
                    auto_click_event(cow1)
                    auto_click_event(cow2)
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
    
                # Reschedule event after its interval has passed
                event_times[name] = calculate_event_time(current_time, intervals[name] - second_lag)
                # Calculate the next execution time based on the original interval, ignoring execution time
                next_event_time = event_time + timedelta(minutes=intervals[name])
                event_times[name] = next_event_time
    
    # Main loop
    while True:
        schedule_events(event_times, second_lag)
        time.sleep(1)  # Check every second for precise execution

if __name__ == "__main__":
    main()
