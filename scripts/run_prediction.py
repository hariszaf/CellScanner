import os
import math
import numpy as np
import pandas as pd
from typing import List

from sklearn.cluster import MiniBatchKMeans
from sklearn.metrics import pairwise_distances
from scipy.stats import entropy

from .helpers import create_file_path
from .illustrations import species_plot, uncertainty_plot, heterogeneity_pie_chart, heterogeneity_bar_plot, gating_plot, create_color_map

# Main function to be called from the worker
def predict(PredictionPanel=None, **kwargs):

    gui = False
    if type(PredictionPanel).__name__ == "PredictionPanel":

        # Attempt to retrieve components from file_panel first
        model, scaler, label_encoder, scaling_constant = get_model_components(PredictionPanel.file_panel)

        # Fallback to train_panel if not found in file_panel
        if model is None:
            model, scaler, label_encoder, scaling_constant = get_model_components(PredictionPanel.train_panel)

        data_df = PredictionPanel.data_df
        output_dir = PredictionPanel.predict_dir

        x_axis_combo = PredictionPanel.x_axis_combo.currentText()
        y_axis_combo = PredictionPanel.y_axis_combo.currentText()
        z_axis_combo = PredictionPanel.z_axis_combo.currentText()
        gating = PredictionPanel.gating_checkbox.isChecked()
        sample = PredictionPanel.sample

        # NOTE: In case a model has just been trained, then the GUI will update the uncertainty threshold
        # (PredictionPanel.uncertainty_threshold) accrordingly
        # Otherwise, a -1 value will be the default value for the uncertainty threshold, in which case,
        # the 0.5*max_entropy will be used as the threshold
        # If the user gives an uncertainty threshold, then it will be used as the threshold -- not as a quantile.
        filter_out_uncertain = PredictionPanel.uncertainty_filtering_checkbox.isChecked()
        uncertainty_threshold = float(PredictionPanel.uncertainty_threshold.value()) if filter_out_uncertain else None

        if gating:
            # Stain 1
            stain1 = PredictionPanel.stain1_combo.currentText()  # It should be the column name
            if stain1 != "Not applicable":
                stain1_relation = PredictionPanel.stain1_relation.currentText()
                stain1_threshold = float(PredictionPanel.stain1_threshold.text())
            else:
                stain1 = None
            # Stain 2
            stain2 = PredictionPanel.stain2_combo.currentText()  # It should be the column name
            if stain2 != "Not applicable":
                stain2_relation = PredictionPanel.stain2_relation.currentText()
                stain2_threshold = float(PredictionPanel.stain2_threshold.text()) if PredictionPanel.stain2_threshold.text() else None
                extra_stains = PredictionPanel.extra_stains
            else:
                stain2 = None
        gui = True

    else:
        sample = kwargs["sample"]
        model = kwargs["model"]
        scaler = kwargs["scaler"]
        label_encoder = kwargs["label_encoder"]
        data_df = kwargs["data_df"]
        output_dir = kwargs["predict_dir"]
        x_axis_combo = kwargs["x_axis_combo"]
        y_axis_combo = kwargs["y_axis_combo"]
        z_axis_combo = kwargs["z_axis_combo"]
        gating = kwargs["gating"]
        scaling_constant = kwargs["scaling_constant"]
        filter_out_uncertain = kwargs["filter_out_uncertain"]
        uncertainty_threshold = kwargs["uncertainty_threshold"]
        if gating:
            Stain1 = kwargs["stain1"]
            stain1, stain1_relation, stain1_threshold = Stain1.channel, Stain1.sign, Stain1.value
            Stain2 = kwargs["stain2"]
            stain2, stain2_relation, stain2_threshold = Stain2.channel, Stain2.sign, Stain2.value
            extra_stains = kwargs["extra_stains"]

    # Get uncertainty threshold
    if filter_out_uncertain:
        number_of_classes = len(label_encoder.classes_)
        max_entropy = math.log(number_of_classes)
        if (uncertainty_threshold < 0 and uncertainty_threshold != -1.0) or uncertainty_threshold > max_entropy:
            raise ValueError("Uncertainty threshold must be between 0 and 1.")

        elif uncertainty_threshold == -1.0:
            uncertainty_threshold = 0.5 * max_entropy
            print(f"Threshold as 0.5 of max entropy: {uncertainty_threshold}, max entropy: {max_entropy}")

    # Predict the species in the coculture file
    predicted_classes, uncertainties, index_to_species = predict_species(
        data_df,
        model,
        scaler,
        label_encoder,
        scaling_constant
    )

    # Convert uncertainties to a Pandas Series
    data_df_pred = data_df.copy()

    # Drop the 'Time' column if it exists
    if 'Time' in data_df_pred.columns:
        data_df_pred = data_df_pred.drop(columns=['Time'])

    # Map prediction indices back to species names
    mapped_predictions = np.vectorize(index_to_species.get)(predicted_classes)

    # Add predictions and uncertainties to the coculture data
    data_df_pred['predictions'] = mapped_predictions

    # Ensure uncertainties is a Series with the same index
    uncertainties = pd.Series(uncertainties, index=data_df_pred.index)

    # Build df with predictions (species names) and uncertainties
    data_df_pred['uncertainties'] = uncertainties  # NOTE: This is the main df to work with

    # Filter out predictions of high entropy
    if filter_out_uncertain:
        data_df_pred.loc[data_df_pred["uncertainties"] > uncertainty_threshold, "predictions"] = "Unknown"

    # Save prediction results and plot the 3D scatter plot
    species_list = list(index_to_species.values())
    save_prediction_results(
        data_df_pred,
        species_list,
        output_dir,
        x_axis_combo, y_axis_combo, z_axis_combo,
        sample=sample,
        scaling_constant=scaling_constant,
        uncertainty_threshold=uncertainty_threshold,
        filter_out_uncertain=filter_out_uncertain
    )

    # Gating -- may return a "state" column mentioning live - dead cells, it may not
    if gating:
        # Apply gating
        gating_df, all_labels = apply_gating(data_df_pred,
            stain1, stain1_relation, stain1_threshold,
            stain2, stain2_relation, stain2_threshold,
            scaling_constant, extra_stains
        )
        # Save gating results
        save_gating_results(
            gating_df, output_dir, sample,
            x_axis_combo, y_axis_combo, z_axis_combo,
            all_labels
        )
        # Perform heterogeneity analysis
        hetero_df = gating_df[gating_df['state'] == 'live'] if "state" in all_labels else gating_df

    else:
        hetero_df = data_df_pred.copy()

    # Calculate heterogeneity
    run_heterogeneity(hetero_df, species_list, output_dir, sample)

    if not gui:
        return data_df_pred


# Functions to be used by the predict()
def predict_species(data_df, model, scaler, label_encoder, scaling_constant):
    """
    Returns:
    - predicted_classes (np.ndarray of ints): The predicted class indices
    - uncertainties (np.ndarray of floats): The entropy values for each prediction
    - index_to_species (dict): A mapping from class index to species name
    """

    # Select only numeric columns
    numeric_cols = data_df.select_dtypes(include=[np.number]).columns

    # Apply arcsinh transformation only to numeric columns
    data_df_arcsinh = data_df.copy()
    data_df_arcsinh[numeric_cols] = np.arcsinh(data_df_arcsinh[numeric_cols] / scaling_constant)

    # Z-standardization
    X_co_scaled = scaler.transform(data_df_arcsinh[numeric_cols])

    # Predict the species
    predictions = model.predict(X_co_scaled)

    # Convert predictions to class labels
    predicted_classes = np.argmax(predictions, axis=1)

    # Calculate entropy for each prediction to represent uncertainty (using scipy.stats.entropy)
    uncertainties = entropy(predictions, axis=1)  # uncertainties example shape (50000,)

    # Create the index-to-species mapping dictionary
    index_to_species = {index: label for index, label in enumerate(label_encoder.classes_)}

    return predicted_classes, uncertainties, index_to_species


def apply_gating(data_df,
                 stain1, stain1_relation, stain1_threshold,
                 stain2=None, stain2_relation=None, stain2_threshold=None,
                 scaling_constant=150,
                 extra_stains=None
    ):

    all_labels = []

    # Copy the DataFrame to not change the original data
    gated_data_df = data_df.copy()

    # Temporarily remove the 'predictions' column to avoid issues with numeric operations
    predictions_column = gated_data_df.pop('predictions') if 'predictions' in gated_data_df.columns else None

    # Apply arcsinh transformation with a cofactor
    cofactor = scaling_constant  # Cofactor

    for column in gated_data_df.select_dtypes(include=[np.number]).columns:
        gated_data_df[column] = np.arcsinh(gated_data_df[column] / cofactor)

    # Reintegrate the 'predictions' column after the arcsinh transformation
    if predictions_column is not None:
        gated_data_df['predictions'] = predictions_column

    if stain1 is not None:

        # Initialize the 'state' column with 'not dead'
        gated_data_df['dead'] = False
        # Apply gating based on the first stain (live/dead)
        if stain1_relation in ['>', 'greater_than']:
            gated_data_df.loc[gated_data_df[stain1] > stain1_threshold, 'dead'] = True
        elif stain1_relation in ['<', 'less_than']:
            gated_data_df.loc[gated_data_df[stain1] < stain1_threshold, 'dead'] = True
        # Sannity check
        stain_sannity_check(gated_data_df, "dead", stain1, stain1_relation, stain1_threshold)
        all_labels.append("dead")

    if stain2 is not None:

        # Initialize the 'state' column with 'not dead'
        gated_data_df['cell'] = False
        # Apply gating based on the first stain (live/dead)
        if stain2_relation in ['>', 'greater_than']:
            gated_data_df.loc[gated_data_df[stain2] > stain2_threshold, 'cell'] = True
        elif stain2_relation in ['<', 'less_than']:
            gated_data_df.loc[gated_data_df[stain2] < stain2_threshold, 'cell'] = True
        # Sannity check
        stain_sannity_check(gated_data_df, "cell", stain2, stain2_relation, stain2_threshold)
        all_labels.append("cell")

    # Apply gating based on the second stain (debris)
    if stain2 and stain2_threshold and stain1:

        gated_data_df["state"] = "debris"

        gated_data_df["state"].loc[
            (gated_data_df["dead"] == False) & (gated_data_df["cell"] == True)
        ] = "live"

        gated_data_df["state"].loc[
            (gated_data_df["dead"] == True) & (gated_data_df["cell"] == True)
        ] = "inactive"

        all_labels.append("state")


    gated_data_df.to_csv("~/Desktop/gated.csv", sep="\t")

    # Apply gating on extra stains
    if extra_stains is not None:

        for channel, details in extra_stains.items():
            sign, threshold, label = details
            # Create the comparison operator dynamically
            condition = gated_data_df[channel] > threshold if sign == ">" else gated_data_df[channel] < threshold
            gated_data_df[label] = condition
            stain_sannity_check(gated_data_df, label, channel, sign, threshold)
            all_labels.append(label)

    return gated_data_df, all_labels


def stain_sannity_check(df, label, channel, sign, threshold):
    """
    Checks if gating applied for a stain returns both True and False cases.
    If not, raises an error so the user refines their thresholds.
    """
    counts = df[label].value_counts()
    if True not in counts.index or False not in counts.index:
        stain_min, stain_max = np.min(df[channel]), np.max(df[channel])
        raise ValueError(
            f"Invalid gating. Please check the gating thresholds."
            f"Stain {channel} ranges between {stain_min} and {stain_max}, while current gating thresholds are {sign} {threshold}."
        )


def save_prediction_results(data_df: pd.DataFrame,
                            species_list: List,
                            output_dir: str,
                            x_axis, y_axis, z_axis,
                            sample: str = None,
                            scaling_constant: int = 150,
                            uncertainty_threshold: float = 0.5,
                            filter_out_uncertain: bool = False
    ):
    # Ensure `data_df` is still a DataFrame and not an ndarray
    if not isinstance(data_df, pd.DataFrame):
        raise ValueError("Expected a DataFrame for `data_df`, but got something else.")

    # Create filenames for the prediction counts CSV and html files
    outfile_predictions = create_file_path(output_dir, sample, 'raw_predictions', 'csv')
    outfile_prediction_counts = create_file_path(output_dir, sample, 'prediction_counts', 'csv')
    plot_path_species = create_file_path(output_dir, sample, '3D_coculture_predictions_species', 'html')

    # Save predictions and prediction counts to a CSV file
    data_df.to_csv(outfile_predictions)
    prediction_counts = data_df['predictions'].value_counts()
    prediction_counts.to_csv(outfile_prediction_counts)
    print("Prediction counts saved to:", outfile_prediction_counts)

    # Calculate and save uncertainty counts by species
    if filter_out_uncertain:
        outfile_uncertainties = create_file_path(output_dir, sample, 'uncertainty_counts', 'csv')
        plot_path_uncertainty = create_file_path(output_dir, sample, '3D_coculture_predictions_uncertainty', 'html')
        uncertainty_counts = data_df.groupby('predictions')['uncertainties'].agg(
            greater_than=lambda x: (x > uncertainty_threshold).sum(),
            less_than=lambda x: (x <= uncertainty_threshold).sum()
        )
        uncertainty_counts.to_csv(outfile_uncertainties)
        print("Uncertainty counts by species saved to:", outfile_uncertainties)

    # Perform arcsinh transformation on numeric columns
    coculture_data_numeric = data_df.drop(columns=['predictions', 'uncertainties'])
    coculture_data_arcsin = np.arcsinh(coculture_data_numeric / scaling_constant)

    # Reintegrate 'predictions' and 'uncertainties' columns
    coculture_data_arcsin['predictions'] = data_df['predictions']
    # coculture_data_arcsin['uncertainties'] = data_df['uncertainties']

    # Dynamically generate color map based on the number of species
    color_map = create_color_map(species_list)

    # Plot 1: Species Plot
    species_plot(coc_arsin_df=coculture_data_arcsin, plot_path=plot_path_species,
                 x_axis=x_axis, y_axis=y_axis, z_axis=z_axis, color_map=color_map
    )
    print("3D scatter plot (Species) saved to:", plot_path_species)

    # Plot 2: Uncertainty Plot
    if filter_out_uncertain:
        coculture_data_arcsin['uncertainties'] = data_df['uncertainties']
        uncertainty_plot(coc_arcsin_df=coculture_data_arcsin, plot_path=plot_path_uncertainty,
                        x_axis=x_axis, y_axis=y_axis, z_axis=z_axis
        )
        print("3D scatter plot (Uncertainty) saved to:", plot_path_uncertainty)


def save_gating_results(gated_data_df, output_dir, sample, x_axis, y_axis, z_axis, all_labels):
    # Create a directory for gating results
    gated_dir = os.path.join(output_dir, 'gated')
    os.makedirs(gated_dir, exist_ok=True)

    # Initialize an empty DataFrame to hold all state counts
    combined_counts_df = pd.DataFrame()

    # Iterate over each species and calculate the state counts
    species_names = gated_data_df['predictions'].unique()
    if "state" in all_labels:
        all_labels.remove("dead") ; all_labels.remove("cell")

    for species in species_names:
        species_df = pd.DataFrame()
        for label in all_labels:
            if label == "state":
                s = gated_data_df[gated_data_df['predictions'] == species][label].value_counts()
            else:
                s = gated_data_df[gated_data_df['predictions'] == species][label].value_counts()
                if s.index[0] == True:
                    s.index = [label, "_".join(["not", label])] if len(s.index) == 2 else [label]
                else:
                    s.index = ["_".join(["not", label]), label] if len(s.index) == 2 else ["_".join(["not", label])] # Default for False case
            s.name = species
            species_df = pd.concat([species_df, s], axis=0)

        combined_counts_df = pd.concat([combined_counts_df, species_df], axis=1)

    # Save the combined state counts to a single CSV file
    combined_counts_df.to_csv(
        os.path.join(gated_dir, "_".join([sample,'gating.csv']))
    )

    # Plot status if both stains provided
    gating_plot(gated_data_df, species_names, x_axis, y_axis, z_axis, gated_dir, sample, all_labels)

    print("3D scatter plot for gated data saved to:", gated_dir)


def run_heterogeneity(df, species_list, output_dir, sample):

    # Create a directory for heterogeneity results
    heterogeneity_dir = os.path.join(output_dir, 'heterogeneity_results')
    os.makedirs(heterogeneity_dir, exist_ok=True)

    hetero_df = df.select_dtypes(include='number')
    hetero_df.drop('uncertainties', axis=1, inplace=True)
    hetero_df['predictions'] = df['predictions']

    # Compute heterogeneity measures for the sample
    try:
        hetero1 = hetero_simple(hetero_df.iloc[:, :-1])
        hetero2 = hetero_mini_batch(hetero_df.iloc[:, :-1])
    except ValueError as e:
        raise ValueError("Error calculating heterogeneity.") from e

    # Create and save heterogeneity plots
    save_heterogeneity_plots(hetero1, hetero2, heterogeneity_dir, sample)
    res_file = "_".join([sample, "heterogeneity_results.txt"])
    hetero_res_file = os.path.join(heterogeneity_dir, res_file)
    with open(hetero_res_file, "w") as f:
        f.write("Species\tSimple Heterogeneity\tMedoid Heterogeneity\n")
        f.write(f"Coculture overall\t{hetero1}\t{hetero2}\n")

    # Compute heterogeneity measures for each species
    for species in species_list:
        df = hetero_df[hetero_df['predictions'] == species]
        try:
            hetero1 = hetero_simple(df.iloc[:, :-1])
            hetero2 = hetero_mini_batch(df.iloc[:, :-1])
            save_heterogeneity_plots(hetero1, hetero2, heterogeneity_dir, sample, species)
        except ValueError as e:
            raise ValueError("Error calculating heterogeneity.") from e
        with open(hetero_res_file, "a") as f:
            f.write(f"{species}\t{hetero1}\t{hetero2}\n")


def hetero_simple(data):
    # Calculate simple heterogeneity as the sum of mean ranges across all channels.
    ranges = data.apply(np.ptp, axis=0)
    return np.sum(ranges.mean())


def hetero_mini_batch(data, type='av_diss'):
    # Use MiniBatchKMeans as an alternative
    try:
        kmeans = MiniBatchKMeans(n_clusters=1, batch_size=3080, n_init=3).fit(data)
    except ValueError:
        raise ValueError(
            "MiniBatchKMeans failed to fit the data."
            ""
        )
    if type == 'diameter':
        # Uses np.max()
        result = np.max(pairwise_distances(kmeans.cluster_centers_[0].reshape(1, -1), data))
    elif type == 'av_diss':
        # Uses np.mean()
        result = np.mean(pairwise_distances(kmeans.cluster_centers_[0].reshape(1, -1), data))
    return result


def save_heterogeneity_plots(hetero1, hetero2, output_dir, sample, species = None):

    # Values corresponding to each measure
    metrics_data = [hetero1, hetero2]
    labels = ['Simple Heterogeneity', 'Medoid Heterogeneity']
    colors = ['#ff9999', '#66b3ff']

    # plot dimensions
    plot_width = 800
    plot_height = 600

    # Pie chart
    heterogeneity_pie_chart(labels, metrics_data, colors, output_dir, sample, species, plot_width, plot_height)

    # Bar chart
    heterogeneity_bar_plot(labels, metrics_data, colors, output_dir, sample, species, plot_width, plot_height)


def merge_prediction_results(output_dir, prediction_type):

    if prediction_type not in ["prediction", "uncertainty"]:
        raise ValueError(f"Please provide a valide prediction_type: 'prediction|uncertainty'")

    dfs = []

    pattern = "_".join([prediction_type, "counts"])

    # Loop through all files in the directory
    for file_name in os.listdir(output_dir):
        if pattern not in file_name:
            continue
        file_path = os.path.join(output_dir, file_name)
        # Read each file as a DataFrame
        df = pd.read_csv(file_path, sep=",")  # Adjust separator if needed
        # Rename the "count" column to the filename (without extension)
        new_column_name = file_name.split(pattern)[0][:-1]
        df = df.rename(columns={"count": new_column_name})
        dfs.append(df)

    # Merge all DataFrames on the "predictions" column
    result = pd.concat(dfs, axis=1).loc[:,~pd.concat(dfs, axis=1).columns.duplicated()]

    # Save the final result to a CSV file
    merged_filename = "".join(["merged_", pattern, ".csv"])
    merged_file = os.path.join(output_dir, merged_filename)
    result.to_csv(merged_file, index=False)


def get_model_components(panel):
    """
    Helper function to retrieve model, scaler, label encoder, and scaling constant from a panel.
    """
    model = getattr(panel, "model", None)
    scaler = getattr(panel, "scaler", None)
    label_encoder = getattr(panel, "le", None)
    scaling_constant = getattr(panel, "scaling_constant", None)
    scaling_constant_value = scaling_constant.value() if scaling_constant else None
    return model, scaler, label_encoder, scaling_constant_value
