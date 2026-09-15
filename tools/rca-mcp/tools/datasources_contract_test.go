package tools

import (
	"testing"
	"time"
)

func TestCatalogDrilldownToolsRegistered(t *testing.T) {
	registered := map[string]bool{}
	for _, tool := range Toolset(Stores{}, time.Time{}, time.Time{}) {
		registered[tool.Name] = true
	}
	required := map[string][]string{"process": {"get_process_snapshot"}, "traces": {"get_trace_spans", "get_trace_activity", "get_trace_summaries"}, "network": {"search_host_data"}}
	for _, finding := range dataSourceFindings("") {
		listed := map[string]bool{}
		for _, name := range finding["tools"].([]string) {
			if !registered[name] {
				t.Errorf("catalog tool %s not registered", name)
			}
			listed[name] = true
		}
		for _, name := range required[finding["domain"].(string)] {
			if !listed[name] {
				t.Errorf("missing drilldown %s", name)
			}
		}
	}
}
