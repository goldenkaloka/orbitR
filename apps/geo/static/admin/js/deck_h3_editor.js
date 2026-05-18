/**
 * deck_h3_editor.js
 * Premium H3-based zone editor using deck.gl
 * Features: Auto-geolocation, Dark Mode aesthetics, Interactive Painting
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

        const mainBtn = createControlBtn('🎨 Paint Zones: OFF', '#fff', '#1a1a1a');
        mainBtn.onclick = () => {
            if (!deckInstance) {
                console.error('Map not initialized yet');
                return;
            }
            isEditing = !isEditing;
            mainBtn.innerText = `🎨 Paint Zones: ${isEditing ? 'ON' : 'OFF'}`;
            mainBtn.style.background = isEditing ? '#4285f4' : '#1a1a1a';
            mainBtn.style.color = '#fff';
            if (mapInstance) {
                mapInstance.dragPan.enable();
                if (isEditing) mapInstance.dragPan.disable();
            }
            updateLayers();
        };

        const locateBtn = createControlBtn('📍 Locate Me', '#fff', '#1a1a1a');
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

        // Initialize MapLibre with a premium Dark Style
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

        deckInstance = new deck.DeckGL({
            container: mapContainerDiv,
            maplibregl: maplibreGlobal,
            initialViewState: {
                longitude: DEFAULT_CENTER[0],
                latitude: DEFAULT_CENTER[1],
                zoom: 13,
                pitch: 0,
                bearing: 0
            },
            controller: true,
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
                
                const centerLat = (ne.lat + sw.lat) / 2;
                const centerLng = (ne.lng + sw.lng) / 2;
                
                const cells = [];
                const latStep = 0.01;
                const lngStep = 0.01;
                
                for (let lat = sw.lat; lat <= ne.lat; lat += latStep) {
                    for (let lng = sw.lng; lng <= ne.lng; lng += lngStep) {
                        const cell = h3.latLngToCell(lat, lng, RESOLUTION);
                        if (cell && !cells.includes(cell)) {
                            cells.push(cell);
                        }
                    }
                }
                return cells;
            }

            function updateLayers() {
                if (!deckInstance) return;
                
                const availableHexes = getVisibleHexagons();
                const availableData = availableHexes.map(hex => ({hex, isSelected: selectedCells.has(hex)}));
                const selectedData = Array.from(selectedCells).map(hex => ({hex}));
                
                const layers = [
                    new deck.H3HexagonLayer({
                        id: 'available-layer',
                        data: availableData,
                        getH3Hexagon: d => d.hex,
                        getFillColor: d => d.isSelected ? [66, 133, 244, 180] : [200, 200, 200, 60],
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
                        if (!isEditing) return;
                        
                        let hex = null;
                        if (info.layer && info.layer.id === 'available-layer') {
                            hex = info.object.hex;
                        } else if (info.coordinate) {
                            hex = h3.latLngToCell(info.coordinate[1], info.coordinate[0], RESOLUTION);
                        }

                        if (hex) {
                            console.log('Clicked cell:', hex);
                            if (selectedCells.has(hex)) {
                                selectedCells.delete(hex);
                            } else {
                                selectedCells.add(hex);
                            }
                            updateLayers();
                            syncToDjango();
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

        // Try auto-locate on start
        setTimeout(autoLocate, 1000);
        updateLayers();
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
