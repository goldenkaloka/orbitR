/**
 * h3_visualizer.js
 * Visualizes H3 hexagons on Django GIS maps in real-time.
 */

(function () {
    'use strict';

    const RESOLUTION = 9; // Match ZONE_RESOLUTION in backend

    function getH3() {
        return window.h3;
    }

    function moduleNameFromMapId(mapDivId) {
        var name = mapDivId;
        if (name.startsWith('id_')) { name = name.slice(3); }
        if (name.endsWith('_map'))  { name = name.slice(0, -4); }
        return 'geodjango_' + name;
    }

    function renderHexagons(moduleName) {
        const geoModule = window[moduleName];
        if (!geoModule || !geoModule.map || !getH3()) return;

        const olMap = geoModule.map;
        const ol = window.ol;
        const h3 = getH3();

        // Find the boundary feature
        const layers = olMap.getLayers().getArray();
        const vectorLayer = layers.find(l => l instanceof ol.layer.Vector && l.getSource().getFeatures().length > 0);
        if (!vectorLayer) return;

        const feature = vectorLayer.getSource().getFeatures()[0];
        const geometry = feature.getGeometry();
        if (!geometry || geometry.getType() !== 'Polygon') return;

        // Convert OL coordinates to H3 format [[lat, lng], ...]
        // OL uses [lng, lat] in Web Mercator or WGS84 depending on projection
        const coords = geometry.getCoordinates()[0].map(c => {
            const lonLat = ol.proj.toLonLat(c);
            return [lonLat[1], lonLat[0]]; // [lat, lng]
        });

        try {
            // Fill polygon with H3 cells
            const cells = h3.polygonToCells(coords, RESOLUTION);

            // Remove existing h3 layer
            olMap.getLayers().getArray()
                .filter(l => l.get('name') === 'h3-hexagons')
                .forEach(l => olMap.removeLayer(l));

            const hexFeatures = cells.map(cell => {
                const boundary = h3.cellToBoundary(cell);
                // Convert back to OL coordinates
                const olCoords = boundary.map(b => ol.proj.fromLonLat([b[1], b[0]]));
                
                const hexFeature = new ol.Feature({
                    geometry: new ol.geom.Polygon([olCoords])
                });
                return hexFeature;
            });

            const h3Source = new ol.source.Vector({ features: hexFeatures });
            const h3Layer = new ol.layer.Vector({
                source: h3Source,
                style: new ol.style.Style({
                    stroke: new ol.style.Stroke({
                        color: 'rgba(66, 133, 244, 0.8)',
                        width: 2
                    }),
                    fill: new ol.style.Fill({
                        color: 'rgba(66, 133, 244, 0.1)'
                    })
                }),
                zIndex: 10
            });
            h3Layer.set('name', 'h3-hexagons');
            olMap.addLayer(h3Layer);

        } catch (e) {
            console.error("H3 Visualization failed", e);
        }
    }

    function init() {
        const mapDivs = document.querySelectorAll('div.olmap[id], div[id$="_map"][id^="id_"]');
        mapDivs.forEach(mapDiv => {
            const moduleName = moduleNameFromMapId(mapDiv.id);
            const geoModule = window[moduleName];
            
            if (geoModule && geoModule.map) {
                const olMap = geoModule.map;
                
                // Watch for geometry changes
                const layers = olMap.getLayers().getArray();
                const vectorLayer = layers.find(l => l instanceof window.ol.layer.Vector);
                if (vectorLayer) {
                    vectorLayer.getSource().on('addfeature', () => renderHexagons(moduleName));
                    vectorLayer.getSource().on('change', () => renderHexagons(moduleName));
                    
                    // Initial render if feature already exists
                    setTimeout(() => renderHexagons(moduleName), 1000);
                }
            }
        });
    }

    // Load H3 library from CDN if not present
    if (!window.h3) {
        const script = document.createElement('script');
        script.src = 'https://unpkg.com/h3-js';
        script.onload = () => {
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', () => setTimeout(init, 1000));
            } else {
                setTimeout(init, 1000);
            }
        };
        document.head.appendChild(script);
    } else {
        setTimeout(init, 1000);
    }

})();
