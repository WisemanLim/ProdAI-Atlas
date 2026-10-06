// Package telemetry 는 OTel trace 초기화다 (FR-OB-TRACE-06). OTEL_EXPORTER_OTLP_ENDPOINT 가 없으면 no-op.
package telemetry

import (
	"context"
	"os"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

// Init 은 전역 TracerProvider·W3C 전파기를 설정하고 종료 함수를 돌려준다.
func Init(ctx context.Context, service, env string) (func(context.Context) error, error) {
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	if os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT") == "" {
		return func(context.Context) error { return nil }, nil
	}
	exp, err := otlptracehttp.New(ctx) // OTEL_EXPORTER_OTLP_* 환경변수를 따른다
	if err != nil {
		return nil, err
	}
	res := resource.NewSchemaless(attribute.String("service.name", service), attribute.String("deployment.environment.name", env),
		attribute.String("atlas.unit_id", "GW"))
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)
	return tp.Shutdown, nil
}
