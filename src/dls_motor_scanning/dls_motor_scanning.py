# Library version specification required for dls libraris
import argparse
import datetime
import time
from math import pow, sqrt

import matplotlib.pyplot as plot
from cothread.catools import FORMAT_TIME, caget, caput

TIMEOUT = 100
PV_VAL = ".VAL"
PV_RBV = ".RBV"
PV_EGU = ".EGU"
PV_UEIP = ".UEIP"
PV_VELO = ".VELO"
PV_ACCL = ".ACCL"


def main():
    parser = argparse.ArgumentParser(
        description="Step scan a motor and measure its positioning performance.\n"
        "The motor is moved from start to stop in fixed steps, and at each\n"
        "step the readback position and the time taken for the move are\n"
        "recorded.\n\n"
        "The tool produces:\n"
        "A txt file of the raw scan data\n"
        "A png plot of position error and move time against step\n"
        "Summary statistics printed to the terminal",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument("motor", type=str, help="Motor record PV to scan")
    parser.add_argument("start", type=float, help="Start position in motor EGUs")
    parser.add_argument("stop", type=float, help="Stop position in motor EGUs")
    parser.add_argument("step", type=float, help="Step size in motor EGUs")
    parser.add_argument(
        "delay", type=float, help="Settling time in seconds to wait after each move"
    )
    parser.add_argument(
        "--extra-pv",
        type=str,
        default=None,
        metavar="PV",
        help="Additional PV to read at each step and plot against position",
    )

    parser.add_argument(
        "--trigger-pv",
        type=str,
        default=None,
        metavar="PV",
        help="Additional PV to trigger between moves",
    )

    parser.add_argument(
        "--trigger-width",
        type=float,
        default=1.0,
        metavar="SECS",
        help="Trigger width (default: 1.0)",
    )

    parser.add_argument(
        "--trigger-post-delay",
        type=float,
        default=0.0,
        metavar="SECS",
        help="Post trigger delay (default: 0.0)",
    )

    parser.add_argument(
        "--timestamp",
        action="store_true",
        help="Add a UTC Timestamp column from the EPICS timestamp of the RBV read",
    )

    parser.add_argument(
        "--no-txt",
        action="store_true",
        help="Do not write the txt file of raw scan data",
    )

    parser.add_argument(
        "--no-png",
        action="store_true",
        help="Do not save the plot as a png file",
    )

    parser.add_argument(
        "--no-plot",
        action="store_true",
        help="Do not display the plot on screen",
    )

    args = parser.parse_args()

    motor = args.motor
    start = args.start
    stop = args.stop
    step = args.step
    delay = args.delay
    extra_pv = args.extra_pv
    trigger_pv = args.trigger_pv
    trigger_width = args.trigger_width
    trigger_post_delay = args.trigger_post_delay
    timestamp = args.timestamp
    no_txt = args.no_txt
    no_png = args.no_png
    no_plot = args.no_plot

    print("Motor step scanning...")

    points = (abs(stop - start)) / step
    print(
        "Moving "
        + motor
        + " from "
        + str(start)
        + " to "
        + str(stop)
        + " in steps of "
        + str(step)
    )
    print("Number of points in the scan: " + str(int(points)))

    # Get the date
    now = datetime.datetime.now()
    thedate = now.strftime("%Y-%m-%d-%H:%M:%S")

    # Make base file name
    filename = (
        "Scan_"
        + motor
        + "_"
        + thedate
        + "_"
        + str(start)
        + "_"
        + str(stop)
        + "_"
        + str(abs(step))
    )

    # Open txt file for writing, unless the export has been suppressed
    file = None if no_txt else open(filename + ".txt", "w")

    def write(text):
        if file is not None:
            file.write(text)

    # Read UEIP, VELO, ACCL and EGU
    ueip = str(caget(motor + PV_UEIP))
    velo = str(caget(motor + PV_VELO))
    accl = str(caget(motor + PV_ACCL))
    egu = str(caget(motor + PV_EGU))

    if stop < start:
        step = step * -1.0

    mean_time = 0
    max_time = 0
    min_time = 0
    mean_pos_error = 0
    max_pos_error = 0
    min_pos_error = 0

    pos_error_array = []
    extra_pv_array = []
    pos_array = []
    time_array = []

    print("Moving to start position of " + str(start))
    caput(motor + PV_VAL, start, wait=True, timeout=TIMEOUT)

    headings = ["Desired", "Actual", "MoveTime"]
    if extra_pv is not None:
        headings.append(extra_pv)
    if timestamp:
        headings.append("Timestamp(UTC)")
    heading = " ".join(headings)
    print(heading)
    write(heading + "\n")

    for i in range(int(points)):
        position = start + ((i + 1) * step)

        # Move a step
        start_time = time.time()
        caput(motor + PV_VAL, position, wait=True, timeout=TIMEOUT)
        end_time = time.time()

        # Wait for motor to settle
        if delay > 0:
            time.sleep(delay)
        if timestamp:
            # FORMAT_TIME augments the value with the record's EPICS timestamp
            value = caget(motor + PV_RBV, format=FORMAT_TIME)
            value_timestamp = datetime.datetime.fromtimestamp(
                value.timestamp, tz=datetime.UTC
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        else:
            value = caget(motor + PV_RBV)
        pos_array.append(value)

        if extra_pv is not None:
            extra_pv_val = caget(extra_pv)
            extra_pv_array.append(extra_pv_val)

        # Calculate position error
        pos_error = position - value
        if i == 0:
            min_pos_error = pos_error
        if pos_error > max_pos_error:
            max_pos_error = pos_error
        if pos_error < min_pos_error:
            min_pos_error = pos_error
        mean_pos_error = mean_pos_error + abs(pos_error)  # use absolute value for mean
        pos_error_array.append(pos_error)

        # Calculate time taken for move
        diff_time = end_time - start_time
        if i == 0:
            min_time = diff_time
        if diff_time > max_time:
            max_time = diff_time
        if diff_time < min_time:
            min_time = diff_time
        mean_time = mean_time + diff_time
        time_array.append(diff_time)

        if trigger_pv is not None:
            caput(trigger_pv, 1, wait=True, timeout=TIMEOUT)
            time.sleep(trigger_width)
            caput(trigger_pv, 0, wait=True, timeout=TIMEOUT)
            time.sleep(trigger_post_delay)

        fields = [str(position), str(value), str(diff_time)]
        if extra_pv is not None:
            fields.append(str(extra_pv_val))
        if timestamp:
            fields.append(str(value_timestamp))
        row = " ".join(fields)
        print(row)
        write(row + "\n")

    if file is not None:
        file.flush()
        file.close()

    # Calculate means
    mean_pos_error = mean_pos_error / points
    mean_time = mean_time / points
    # Calculate standard deviation
    sd_pos = 0
    sd_time = 0
    for i in range(int(points)):
        sd_pos = sd_pos + pow((abs(pos_error_array[i]) - mean_pos_error), 2)
        sd_time = sd_time + pow((time_array[i] - mean_time), 2)
    sd_pos = sqrt(sd_pos / points)
    sd_time = sqrt(sd_time / points)
    # Calculate error on mean
    pos_mean_error = sd_pos / sqrt(points)
    time_mean_error = sd_time / sqrt(points)

    print("\n**********************************************")
    print(
        "  Moving "
        + motor
        + " from "
        + str(start)
        + " to "
        + str(stop)
        + " in steps of "
        + str(step)
    )
    print("  Date: " + thedate)
    print("  Number of points in the scan: " + str(points))
    print("  UEIP:" + ueip + " VELO:" + velo + " ACCL:" + accl)
    print("**********************************************")
    print("Time taken for moves:")
    print("  Mean: " + str(mean_time) + " +/- " + str(time_mean_error) + " secs")
    print("  Standard Deviation: " + str(sd_time))
    print("  Min: " + str(min_time) + " secs")
    print("  Max: " + str(max_time) + " secs")
    print("**********************************************")
    print(
        "Position error magnitude at the end of each move "
        "(taking into account settling time delay)"
    )
    print("  Mean: " + str(mean_pos_error) + " +/- " + str(pos_mean_error))
    print("  Standard Deviation: " + str(sd_pos))
    print("  Min Pos Error: " + str(min_pos_error))
    print("  Max Pos Error: " + str(max_pos_error))
    print("  Delay: " + str(delay) + " secs\n")

    # Plot data
    # Plot data for position

    if not (no_png and no_plot):
        plot_size = 200
        if extra_pv is not None:
            plot_size += 100

        fig = plot.figure(1, figsize=(8.27, 11.69))
        fig.suptitle(
            "Step Scanning "
            + str(motor)
            + "\nStart="
            + str(start)
            + " Stop="
            + str(stop)
            + " Step="
            + str(abs(step))
            + " Delay="
            + str(delay)
            + "\n"
            + thedate
            + "\n UEIP:"
            + ueip
            + " VELO:"
            + velo
            + " ACCL:"
            + accl,
            fontsize=14,
        )
        plot.subplot(plot_size + 11)
        plot.plot(pos_error_array)
        text_pos_offset = 0
        if abs(min_pos_error) > abs(max_pos_error):
            if min_pos_error < 0:
                plot.ylim(
                    min_pos_error + (min_pos_error / 10),
                    -min_pos_error - (min_pos_error / 10),
                )
            else:
                plot.ylim(
                    -min_pos_error - (min_pos_error / 10),
                    min_pos_error + (min_pos_error / 10),
                )
            text_pos_offset = abs(min_pos_error) - abs(min_pos_error) / 10
        else:
            if max_pos_error < 0:
                plot.ylim(
                    max_pos_error + (max_pos_error / 10),
                    -max_pos_error - (max_pos_error / 10),
                )
            else:
                plot.ylim(
                    -max_pos_error - (max_pos_error / 10),
                    max_pos_error + (max_pos_error / 10),
                )
            text_pos_offset = abs(max_pos_error) - abs(max_pos_error) / 10
        plot.ylabel("Demand Position - Actual Position (" + egu + ")")
        plot.xlabel("Step")
        plot_text = (
            "Mean="
            + str(mean_pos_error)
            + "+/-"
            + str(pos_mean_error)
            + "\n"
            + "SD="
            + str(sd_pos)
        )
        plot.text(
            points / 5,
            text_pos_offset,
            plot_text,
            horizontalalignment="left",
            verticalalignment="top",
        )
        plot.axhline(y=0, xmin=0, xmax=points, linestyle="--", color="black")
        # Plot data for time
        plot.subplot(plot_size + 12)
        plot.ylabel("Time Taken For Move (Seconds)")
        plot.xlabel("Step")
        plot_text = (
            "Mean="
            + str(mean_time)
            + "+/-"
            + str(time_mean_error)
            + "\n"
            + "SD="
            + str(sd_time)
        )
        plot.text(
            points / 5,
            0 + (max_time / 6),
            plot_text,
            horizontalalignment="left",
            verticalalignment="top",
        )
        plot.plot(time_array, color="r")
        plot.ylim(0, max_time + (max_time / 10))

        if extra_pv is not None:
            plot.subplot(313)
            plot.ylabel(extra_pv)
            plot.plot(pos_array, extra_pv_array, color="b")
            plot.ylim(min(extra_pv_array), max(extra_pv_array))
            plot.xlim(min(pos_array), max(pos_array))
            plot.xlabel("Actual Position (mm)")

        if not no_png:
            plot.savefig(filename + ".png")
            print("  Plot saved in " + str(filename) + ".png")

    if not no_txt:
        print("  Data saved in " + str(filename) + ".txt\n")

    if not no_plot:
        plot.show()


if __name__ == "__main__":
    from pkg_resources import require

    require("numpy")
    require("cothread")
    require("matplotlib")
    main()
