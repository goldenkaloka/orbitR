/**
 * deck_h3_editor.js
 * Premium H3-based zone editor using deck.gl
 * Features: Auto-geolocation, Unfold Teal aesthetics, Interactive Brush Painting, Loading Existing Boundaries
 */

(function () {
    'use strict';

    const RESOLUTION = 9;
    const DEFAULT_CENTER = [39.2026, -6.7924]; // Dar es Salaam
    
    let deckInstance = null;
    let selectedCells = new Set();
    let isEditing = false;
    let mapInstance = null;

    // Load dependencies
    const scripts = [
        'https://unpkg.com/deck.gl@8.9.33/dist.min.js',
        'https://unpkg.com/h3-js@4.1.0/dist/h3-js.umd.js',
        'https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.js'
    ];

    const styles = [
        'https://unpkg.com/maplibre-gl@3.6.2/dist/maplibre-gl.css',
        'https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap'
    ];

    function loadDependencies(callback) {
        let loaded = 0;
        styles.forEach(url => {
            const link = document.createElement('link');
            link.rel = 'stylesheet';
            link.href = url;
            document.head.appendChild(link);
        });

        scripts.forEach(url => {
            const script = document.createElement('script');
            script.src = url;
            script.onload = () => {
                loaded++;
                if (loaded === scripts.length) callback();
            };
            document.head.appendChild(script);
        });
    }

    function initDeckEditor(mapContainer, textArea) {
        // Parse existing saved boundaries from Django field on startup
        if (textArea.value) {
            try {
                const geojson = JSON.parse(textArea.value);
                if (geojson) {
                    let polygons = [];
                    if (geojson.type === 'MultiPolygon') {
                        polygons = geojson.coordinates;
                    } else if (geojson.type === 'Polygon') {
                        polygons = [geojson.coordinates];
                    }
                    
                    polygons.forEach(polygonCoords => {
                        // GeoJSON coords: [ [ [lng, lat], ... ] ]
                        // H3 expects coordinates as [ [lat, lng], ... ]
                        const exterior = polygonCoords[0].map(pt => [pt[1], pt[0]]);
                        
                        let cells = [];
                        // Check H3 version API
                        if (typeof h3.polygonToCells === 'function') {
                            cells = h3.polygonToCells(exterior, RESOLUTION);
                        } else if (typeof h3.polyfill === 'function') {
                            cells = h3.polyfill(exterior, RESOLUTION);
                        }
                        
                        cells.forEach(cell => selectedCells.add(cell));
                    });
                    console.log(`Loaded ${selectedCells.size} existing hexagons from saved boundaries.`);
                }
            } catch (err) {
                console.error("Error parsing existing boundary GeoJSON:", err);
            }
        }

        // Create a wrapper for the new premium experience
        const wrapper = document.createElement('div');
        wrapper.className = 'premium-map-wrapper';
        Object.assign(wrapper.style, {
            width: '100%',
            height: '600px',
            position: 'relative',
            marginTop: '20px',
            marginBottom: '20px',
            borderRadius: '12px',
            boxShadow: '0 10px 30px rgba(0,0,0,0.15)',
            overflow: 'hidden',
            fontFamily: "'Inter', sans-serif",
            border: '1px solid #333'
        });

        // Add custom controls
        const controls = document.createElement('div');
        Object.assign(controls.style, {
            position: 'absolute',
            top: '20px',
            left: '20px',
            zIndex: '10',
            display: 'flex',
            flexDirection: 'column',
            gap: '10px'
        });

        const mainBtn = createControlBtn('🎨 Paint Brush: OFF', '#fff', '#1a1a1a');
        mainBtn.onclick = () => {
            if (!deckInstance) {
                console.error('Map not initialized yet');
                return;
            }
            isEditing = !isEditing;
            mainBtn.innerText = `🎨 Paint Brush: ${isEditing ? 'ON' : 'OFF'}`;
            mainBtn.style.background = isEditing ? '#14b8a6' : '#1a1a1a'; // Orbitr Teal
            mainBtn.style.color = '#fff';
            
            // Disable dragPan on deck.gl's controller during paint mode!
            deckInstance.setProps({
                controller: {
                    dragPan: !isEditing,
                    dragRotate: false,
                    doubleClickZoom: true,
                    scrollZoom: true
                }
            });
            
            if (mapInstance) {
                if (isEditing) {
                    mapInstance.dragPan.disable(); // Disable map dragging so brush drag works
                } else {
                    mapInstance.dragPan.enable();
                }
            }
            updateLayers();
        };

        const locateBtn = createControlBtn('📍 Center on Me', '#fff', '#1a1a1a');
        locateBtn.onclick = () => {
            if (!deckInstance) {
                console.error('Map not initialized yet');
                return;
            }
            autoLocate();
        };

        const clearBtn = createControlBtn('🗑️ Clear All', '#fff', '#1a1a1a');
        clearBtn.onclick = () => {
            if (!deckInstance) {
                console.error('Map not initialized yet');
                return;
            }
            if (confirm("Are you sure you want to clear all hexagons?")) {
                selectedCells.clear();
                updateLayers();
                syncToDjango();
            }
        };

        controls.appendChild(mainBtn);
        controls.appendChild(locateBtn);
        controls.appendChild(clearBtn);
        wrapper.appendChild(controls);

        // MapLibre container
        const mapContainerDiv = document.createElement('div');
        mapContainerDiv.style.width = '100%';
        mapContainerDiv.style.height = '100%';
        wrapper.appendChild(mapContainerDiv);

        // Hide original map container to clean up space
        mapContainer.style.display = 'none';
        mapContainer.parentNode.insertBefore(wrapper, mapContainer);

        // Initialize MapLibre with Voyager light map
        var maplibreGlobal = window.maplibregl || window.maplibre;
        if (!maplibreGlobal) {
            console.error('MapLibre GL not loaded');
            return;
        }

        mapInstance = new maplibreGlobal.Map({
            container: mapContainerDiv,
            style: 'https://basemaps.cartocdn.com/gl/voyager-gl-style/style.json',
            center: DEFAULT_CENTER,
            zoom: 13,
            pitch: 0
        });

        // Set map center if there are pre-existing cells
        if (selectedCells.size > 0) {
            const firstCell = Array.from(selectedCells)[0];
            const cellCenter = h3.cell_to_latlng(firstCell);
            mapInstance.setCenter([cellCenter[1], cellCenter[0]]);
        }

        deckInstance = new deck.DeckGL({
            container: mapContainerDiv,
            maplibregl: maplibreGlobal,
            initialViewState: {
                longitude: selectedCells.size > 0 ? h3.cell_to_latlng(Array.from(selectedCells)[0])[1] : DEFAULT_CENTER[0],
                latitude: selectedCells.size > 0 ? h3.cell_to_latlng(Array.from(selectedCells)[0])[0] : DEFAULT_CENTER[1],
                zoom: 13,
                pitch: 0,
                bearing: 0
            },
            controller: {
                dragPan: true,
                dragRotate: false,
                doubleClickZoom: true,
                scrollZoom: true
            },
            onViewStateChange: ({viewState}) => {
                if (mapInstance) {
                    mapInstance.jumpTo({
                        center: [viewState.longitude, viewState.latitude],
                        zoom: viewState.zoom,
                        bearing: viewState.bearing,
                        pitch: viewState.pitch
                    });
                }
            },
            layers: []
        });

        function createControlBtn(text, color, bg) {
            const b = document.createElement('button');
            b.type = 'button';
            b.innerText = text;
            Object.assign(b.style, {
                padding: '10px 16px',
                border: 'none',
                borderRadius: '8px',
                background: bg,
                color: color,
                cursor: 'pointer',
                fontSize: '14px',
                fontWeight: '600',
                boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
                transition: 'all 0.2s ease',
                display: 'flex',
                alignItems: 'center',
                gap: '8px'
            });
            b.onmouseover = () => b.style.transform = 'translateY(-2px)';
            b.onmouseout = () => b.style.transform = 'translateY(0)';
            return b;
        }

        function autoLocate() {
            if (!deckInstance || !mapInstance) return;
            if (navigator.geolocation) {
                navigator.geolocation.getCurrentPosition(pos => {
                    const {latitude, longitude} = pos.coords;
                    mapInstance.flyTo({ center: [longitude, latitude], zoom: 14, duration: 1000 });
                    deckInstance.setProps({
                        initialViewState: {
                            longitude,
                            latitude,
                            zoom: 14,
                            transitionDuration: 1000,
                            transitionInterpolator: new deck.FlyToInterpolator()
                        }
                    });
                });
            }
        }

        function getVisibleHexagons() {
            if (!mapInstance) return [];
            const bounds = mapInstance.getBounds();
            if (!bounds) return [];
            
            const ne = bounds.getNorthEast();
            const sw = bounds.getSouthWest();
            
            const zoom = mapInstance.getZoom();
            if (zoom < 11) return []; // Don't compute cells if zoomed out too far for performance
            
            const cells = [];
            // Step size adapts based on zoom to ensure perfect viewport capture without freezing
            const latStep = zoom >= 14 ? 0.003 : 0.006;
            const lngStep = zoom >= 14 ? 0.003 : 0.006;
            
            for (let lat = sw.lat; lat <= ne.lat; lat += latStep) {
                for (let lng = sw.lng; lng <= ne.lng; lng += lngStep) {
                    let cell = null;
                    if (typeof h3.latLngToCell === 'function') {
                        cell = h3.latLngToCell(lat, lng, RESOLUTION);
                    } else if (typeof h3.geoToH3 === 'function') {
                        cell = h3.geoToH3(lat, lng, RESOLUTION);
                    }
                    if (cell && !cells.includes(cell)) {
                        cells.push(cell);
                    }
                }
            }
            return cells;
        }

        function updateLayers() {
            if (!deckInstance) return;
            
            const visibleHexes = getVisibleHexagons();
            // Union visible viewport hexagons and all selected hexagons so selected ones never disappear!
            const allHexes = new Set([...visibleHexes, ...selectedCells]);
            
            const data = Array.from(allHexes).map(hex => ({
                hex, 
                isSelected: selectedCells.has(hex)
            }));
            
            const layers = [
                new deck.H3HexagonLayer({
                    id: 'hexagon-layer',
                    data: data,
                    getH3Hexagon: d => d.hex,
                    // Vibrant Teal matching Unfold primary branding [20, 184, 166]
                    getFillColor: d => d.isSelected ? [20, 184, 166, 180] : [200, 200, 200, 60],
                    getLineColor: d => d.isSelected ? [255, 255, 255, 255] : [150, 150, 150, 120],
                    lineWidthMinPixels: 2,
                    pickable: true,
                    extruded: false,
                    filled: true
                })
            ];

            deckInstance.setProps({
                layers,
                onClick: (info) => {
                    // Precise clicking allows toggling hexagons
                    let hex = null;
                    if (info.layer && info.layer.id === 'hexagon-layer') {
                        hex = info.object.hex;
                    } else if (info.coordinate) {
                        const latLngToCell = h3.latLngToCell || h3.geoToH3;
                        hex = latLngToCell(info.coordinate[1], info.coordinate[0], RESOLUTION);
                    }

                    if (hex) {
                        if (selectedCells.has(hex)) {
                            selectedCells.delete(hex);
                        } else {
                            selectedCells.add(hex);
                        }
                        updateLayers();
                        syncToDjango();
                    }
                },
                onHover: (info) => {
                    // Paint brush mode: paint hexagons continuously while holding down the primary mouse button (left-click)
                    if (!isEditing) return;
                    
                    const mouseEvent = info.srcEvent;
                    if (mouseEvent && mouseEvent.buttons === 1) { // Left mouse button held down
                        let hex = null;
                        if (info.layer && info.layer.id === 'hexagon-layer') {
                            hex = info.object.hex;
                        } else if (info.coordinate) {
                            const latLngToCell = h3.latLngToCell || h3.geoToH3;
                            hex = latLngToCell(info.coordinate[1], info.coordinate[0], RESOLUTION);
                        }

                        if (hex && !selectedCells.has(hex)) {
                            selectedCells.add(hex);
                            updateLayers();
                            syncToDjango();
                        }
                    }
                }
            });
        }

        function syncToDjango() {
            if (selectedCells.size === 0) {
                textArea.value = '';
                return;
            }
            const multiPoly = h3.cellsToMultiPolygon(Array.from(selectedCells), true);
            const geojson = {
                type: "MultiPolygon",
                coordinates: multiPoly
            };
            textArea.value = JSON.stringify(geojson);
            textArea.dispatchEvent(new Event('change', { bubbles: true }));
        }

        // Try auto-locate on start if no cells exist
        if (selectedCells.size === 0) {
            setTimeout(autoLocate, 1000);
        }
        updateLayers();
        
        // Connect event listener to update hexagons when viewport pans/zooms
        mapInstance.on('moveend', () => {
            updateLayers();
        });
    }

    function start() {
        var mapContainer = document.querySelector('[id$="boundary_map"]') ||
                           document.querySelector('[id$="boundary-div"]') ||
                           document.querySelector('.leaflet-container');
        var textArea = document.querySelector('#id_boundary');

        if (textArea && mapContainer) {
            initDeckEditor(mapContainer, textArea);
        } else {
            if (!textArea) console.error('Zone editor: textarea#id_boundary not found');
            if (!mapContainer) console.error('Zone editor: map container not found. Check Leaflet is loaded.');
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { setTimeout(start, 1500); });
    } else {
        setTimeout(start, 1500);
    }

    loadDependencies(start);

})();
