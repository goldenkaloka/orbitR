/**
 * geo_locate.js
 * Injects a "Use My Location" button into every Django GIS OpenLayers
 * map widget in the admin. On click it:
 *   1. Requests the browser's Geolocation API
 *   2. Pans/zooms the OL map to the user's coordinates
 *   3. Drops a styled blue dot marker at that position
 *
 * Django's OpenLayersWidget stores each map instance as:
 *   window.geodjango_<fieldname>   →  { map: OLMapInstance, ... }
 * The map div id is:  id_<fieldname>_map
 */

(function () {
    'use strict';

    /* ------------------------------------------------------------------ */
    /*  Derive the geodjango module name from the OL map div id            */
    /*  e.g.  "id_boundary_map"    →  "geodjango_boundary"                */
    /*        "id_geo_location_map" → "geodjango_geo_location"            */
    /* ------------------------------------------------------------------ */
    function moduleNameFromMapId(mapDivId) {
        var name = mapDivId;
        if (name.startsWith('id_')) { name = name.slice(3); }
        if (name.endsWith('_map'))  { name = name.slice(0, -4); }
        return 'geodjango_' + name;
    }

    /* ------------------------------------------------------------------ */
    /*  Inject the locate button into one map container div                */
    /* ------------------------------------------------------------------ */
    function injectButton(mapDiv) {
        // Guard: don't double-inject
        if (mapDiv.querySelector('.geo-locate-btn')) { return; }

        var moduleName = moduleNameFromMapId(mapDiv.id);

        /* -- Button element -- */
        var btn = document.createElement('button');
        btn.type        = 'button';
        btn.className   = 'geo-locate-btn';
        btn.title       = 'Pan map to your current GPS location';
        btn.innerHTML   = '&#128205; Use My Location';

        Object.assign(btn.style, {
            position:   'absolute',
            top:        '10px',
            right:      '10px',
            zIndex:     '1000',
            background: '#fff',
            border:     '2px solid #0078d4',
            borderRadius: '6px',
            padding:    '7px 14px',
            cursor:     'pointer',
            fontSize:   '13px',
            fontWeight: '600',
            color:      '#0078d4',
            boxShadow:  '0 2px 8px rgba(0,0,0,0.25)',
            transition: 'all 0.2s ease',
        });

        btn.addEventListener('mouseenter', function () {
            btn.style.background = '#0078d4';
            btn.style.color      = '#fff';
        });
        btn.addEventListener('mouseleave', function () {
            btn.style.background = '#fff';
            btn.style.color      = '#0078d4';
        });

        /* The OL map div uses position:relative already; guarantee it */
        mapDiv.style.position = 'relative';
        mapDiv.appendChild(btn);

        /* -- Click handler -- */
        btn.addEventListener('click', function () {
            if (!navigator.geolocation) {
                alert('Geolocation is not supported by your browser.');
                return;
            }

            btn.innerHTML = '&#8987; Locating\u2026';
            btn.disabled  = true;

            navigator.geolocation.getCurrentPosition(
                function onSuccess(position) {
                    var lat = position.coords.latitude;
                    var lng = position.coords.longitude;

                    /* Resolve the Django map module */
                    var geoModule = window[moduleName];
                    if (!geoModule || !geoModule.map) {
                        alert('Map is not ready yet \u2014 please try again.');
                        reset();
                        return;
                    }

                    var olMap = geoModule.map;
                    var view  = olMap.getView();
                    var ol    = window.ol;

                    /* Transform WGS-84 → Web Mercator (EPSG:3857) */
                    var coords;
                    if (ol && ol.proj && ol.proj.fromLonLat) {
                        coords = ol.proj.fromLonLat([lng, lat]);
                    } else {
                        /* Manual Mercator fallback */
                        var x = lng * 20037508.34 / 180;
                        var y = Math.log(
                            Math.tan((90 + lat) * Math.PI / 360)
                        ) / (Math.PI / 180) * 20037508.34 / 180;
                        coords = [x, y];
                    }

                    /* Fly to the location */
                    view.animate({ center: coords, zoom: 17, duration: 900 });

                    /* Remove any previous location layer */
                    olMap.getLayers().getArray()
                        .filter(function (l) { return l.get('name') === 'user-location'; })
                        .forEach(function (l) { olMap.removeLayer(l); });

                    /* Add a blue dot marker */
                    if (ol && ol.Feature && ol.geom && ol.style && ol.layer && ol.source) {
                        var dot = new ol.Feature({
                            geometry: new ol.geom.Point(coords)
                        });

                        dot.setStyle(new ol.style.Style({
                            image: new ol.style.Circle({
                                radius: 9,
                                fill:   new ol.style.Fill({ color: '#4285f4' }),
                                stroke: new ol.style.Stroke({ color: '#ffffff', width: 2.5 })
                            })
                        }));

                        /* Pulsing outer ring */
                        var ring = new ol.Feature({
                            geometry: new ol.geom.Point(coords)
                        });
                        ring.setStyle(new ol.style.Style({
                            image: new ol.style.Circle({
                                radius: 16,
                                fill:   new ol.style.Fill({ color: 'rgba(66,133,244,0.20)' }),
                                stroke: new ol.style.Stroke({ color: 'rgba(66,133,244,0.60)', width: 1.5 })
                            })
                        }));

                        var locationLayer = new ol.layer.Vector({
                            source: new ol.source.Vector({ features: [ring, dot] }),
                            zIndex: 999
                        });
                        locationLayer.set('name', 'user-location');
                        olMap.addLayer(locationLayer);
                    }

                    btn.innerHTML = '&#10003; Located!';
                    btn.style.borderColor = '#34a853';
                    btn.style.color       = '#34a853';
                    setTimeout(reset, 2500);
                },

                function onError(err) {
                    var messages = {
                        1: 'Location permission was denied.',
                        2: 'Location is currently unavailable.',
                        3: 'Location request timed out.',
                    };
                    alert(messages[err.code] || 'Could not get your location.');
                    reset();
                },

                { enableHighAccuracy: true, timeout: 12000, maximumAge: 0 }
            );

            function reset() {
                btn.innerHTML = '&#128205; Use My Location';
                btn.disabled  = false;
                btn.style.borderColor = '#0078d4';
                btn.style.color       = '#0078d4';
            }
        });
    }

    /* ------------------------------------------------------------------ */
    /*  Find all OL map containers on the page and inject the button       */
    /*  Django renders them as:  <div id="id_<field>_map" class="olmap">  */
    /* ------------------------------------------------------------------ */
    function initAll() {
        var mapDivs = document.querySelectorAll('div.olmap[id]');
        if (mapDivs.length === 0) {
            /* Fallback: some Django versions omit the class */
            mapDivs = document.querySelectorAll('div[id$="_map"][id^="id_"]');
        }
        mapDivs.forEach(injectButton);
    }

    /* Run after Django's own map JS has had time to initialise */
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () {
            setTimeout(initAll, 600);
        });
    } else {
        setTimeout(initAll, 600);
    }

})();
