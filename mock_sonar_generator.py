import geopandas as gpd
import pandas as pd
import numpy as np
from shapely.geometry import Point
import argparse
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_SHAPEFILE = PROJECT_DIR / "data" / "pond_boundary.shp"
DEFAULT_OUTPUT = PROJECT_DIR / "data" / "mock_sonar_survey.csv"

def generate_mock_sonar(shapefile_path, output_csv, num_points=500, max_depth=6.5):
    print(f"Loading {shapefile_path}...")
    try:
        gdf = gpd.read_file(shapefile_path)
    except Exception as e:
        print(f"Error loading shapefile: {e}")
        return

    # Ensure EPSG:4326 for lat/lon output
    if gdf.crs and gdf.crs.to_string() != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")
        
    pond_poly = gdf.geometry.unary_union
    
    minx, miny, maxx, maxy = pond_poly.bounds
    
    points = []
    print(f"Generating {num_points} theoretical soundings...")
    
    # Generate random points inside the polygon
    while len(points) < num_points:
        p = Point(np.random.uniform(minx, maxx), np.random.uniform(miny, maxy))
        if pond_poly.contains(p):
            # Calculate distance to boundary for a mock depth (further from shore = deeper)
            dist = pond_poly.boundary.distance(p)
            points.append((p, dist))
            
    # Extract distances to scale them correctly
    distances = np.array([pt[1] for pt in points])
    if len(distances) == 0:
        print("Could not generate points within the polygon.")
        return
        
    max_dist = distances.max()
    
    results = []
    for p, dist in points:
        # Scale depth so the center is deepest
        base_depth = (dist / max_dist) * max_depth
        # Add some natural noise (±15%)
        depth = base_depth * np.random.uniform(0.85, 1.15)
        # Ensure minimum depth
        depth = max(0.2, round(depth, 2))
        
        # Random fish detection (e.g., 10% chance)
        fish = bool(np.random.choice([True, False], p=[0.10, 0.90]))
        
        # GeoPandas Point x is lon, y is lat
        results.append({
            'lat': round(p.y, 6),
            'lon': round(p.x, 6),
            'depth_m': depth,
            'fish_detected': fish
        })
        
    df = pd.DataFrame(results)
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(output_path, index=False)
    print(f"Successfully generated {output_path} with {len(df)} mock records.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate theoretical Deeper Pro 2 CSV from a pond shapefile.")
    parser.add_argument("shp", nargs="?", default=str(DEFAULT_SHAPEFILE), help="Path to input shapefile (.shp). Default: data/pond_boundary.shp")
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT), help="Output CSV filename. Default: data/mock_sonar_survey.csv")
    parser.add_argument("--points", type=int, default=600, help="Number of soundings to generate")
    parser.add_argument("--depth", type=float, default=3.5, help="Max depth in meters")
    
    args = parser.parse_args()
    generate_mock_sonar(args.shp, args.out, args.points, args.depth)
