import argparse

import numpy as np
import pandas as pd


def main():
    parser = argparse.ArgumentParser(
        description="Create 5th order polynomial to convert an unscaled feedback "
        "device into EGUs.\n"
        "For example, converting a potentiometer from raw ADC bits to EGUs.\n"
        "The CSV should have 2 data sets:\n"
        "Scaled position (float)\n"
        "Unscaled postion (int)\n\n"
        "The tool produces:\n"
        "A Builder calc record XMl entry\n"
        "An excel formula to convert raw to EGUs",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "csvPath", type=str, help="Path to the CSV file containing the scanned data"
    )
    parser.add_argument(
        "rawInputPV",
        type=str,
        nargs="?",
        default="inputPV",
        help="Raw input PV from ADC (default: inputPV)",
    )
    parser.add_argument(
        "excelRawPotFirstCell",
        type=str,
        nargs="?",
        default="A1",
        help="First cell in Excel for raw feedback (default: A1)",
    )

    args = parser.parse_args()

    # # Load data from CSV
    df = pd.read_csv(args.csvPath)

    # Identify columns with integer and float data
    int_column = None
    float_column = None

    for column in df.columns:
        if pd.api.types.is_integer_dtype(df[column]):
            int_column = column
        elif pd.api.types.is_float_dtype(df[column]):
            float_column = column

    # Ensure both columns were found
    if int_column is None or float_column is None:
        raise ValueError(
            "CSV file must contain at least one integer column and one float column."
        )

    # Extract columns
    raw_feedback = df[int_column].values
    encoder = df[float_column].values

    # Fit a 5th-degree polynomial: encoder = f(raw_feedback)
    coefficients = np.polyfit(raw_feedback, encoder, 5)

    # Assign coefficients to named variables (highest degree first)
    g, f, e, d, c, b = coefficients  # Corresponds to A^5 down to A^0

    # Display named variables
    print(f"B = {b:.10e}")
    print(f"C = {c:.10e}")
    print(f"D = {d:.10e}")
    print(f"E = {e:.10e}")
    print(f"F = {f:.10e}")
    print(f"G = {g:.10e}")

    cell = args.excelRawPotFirstCell

    calc_record = (
        f'<records.calc B="{b:.10e}" C="{c:.10e}" '
        'CALC="((A^0)*B)+((A^1)*C)+((A^2)*D)+((A^3)*E)+((A^4)*F)+((A^5)*G)" '
        f'D="{d:.10e}" E="{e:.10e}" EGU="mm" F="{f:.10e}" G="{g:.10e}" '
        f'INPA="{args.rawInputPV}" PREC="4" SCAN=".1 second" name="" record=""/>'
    )
    excel_form = (
        f"=({b:.10e}*POWER({cell},0))"
        f"+({c:.10e}*POWER({cell},1))"
        f"+({d:.10e}*POWER({cell},2))"
        f"+({e:.10e}*POWER({cell},3))"
        f"+({f:.10e}*POWER({cell},4))"
        f"+({g:.10e}*POWER({cell},5))"
    )

    print("\nEPICS calc record:")
    print(calc_record)

    print("\nExcel formula:")
    print(excel_form)
